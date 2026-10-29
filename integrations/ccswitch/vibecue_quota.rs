//! Opt-in local, credential-free quota export for VibeCue.
//!
//! CC Switch remains the sole owner of credentials, refresh serialization and
//! account selection. No tokens or raw upstream errors enter the export.

use std::io::Write;
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Value};
use crate::proxy::providers::codex_oauth_auth::CodexOAuthManager;
use crate::services::subscription::SubscriptionQuota;

const ENABLE_FILE: &str = "vibecue-quota-sharing.enabled";
const EXPORT_FILE: &str = "vibecue-codex-quota.json";
const SOURCE: &str = "ccswitch_quota_bridge";

/// Called once during setup. The marker is checked each cycle, so sharing can
/// be disabled without changing or removing any authorization.
pub(crate) fn start(manager: Arc<CodexOAuthManager>) {
    tauri::async_runtime::spawn(async move {
        let mut ticks = tokio::time::interval(Duration::from_secs(60));
        ticks.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            ticks.tick().await;
            let directory = crate::config::get_app_config_dir();
            let marker = directory.join(ENABLE_FILE);
            if !regular_file(&marker) { continue; }
            let accounts = manager.list_accounts().await;
            let deadline = tokio::time::Instant::now() + Duration::from_secs(20);
            let gate = Arc::new(tokio::sync::Semaphore::new(4));
            let mut pending = Vec::new();
            // Start independent accounts concurrently. The cap is shared with
            // the reader and prevents an unexpected store from creating a herd.
            for account in accounts.into_iter().take(20) {
                let workspace = manager.chatgpt_account_id_for_account(&account.id).await.ok();
                let identity = (account.id.clone(), workspace, account.login);
                let worker_manager = manager.clone();
                let gate = gate.clone();
                let task = tokio::spawn(async move {
                    let Ok(Ok(_permit)) = tokio::time::timeout_at(deadline, gate.acquire_owned()).await else {
                        return None;
                    };
                    let result = crate::commands::query_codex_oauth_quota_for(&worker_manager, &account.id).await;
                    let after = worker_manager.chatgpt_account_id_for_account(&account.id).await.ok();
                    Some((result, after))
                });
                pending.push((identity, task));
            }
            let mut records = Vec::new();
            for ((local, workspace, login), mut task) in pending {
                let (quota, code) = match tokio::time::timeout_at(deadline, &mut task).await {
                    Ok(Ok(Some((Ok(value), after)))) if workspace == after => (Some(value), ""),
                    Ok(Ok(Some((Ok(_), _)))) => (None, "identity_unavailable"),
                    Ok(Ok(Some((Err(_), _)))) | Ok(Err(_)) => (None, "query_failed"),
                    Ok(Ok(None)) | Err(_) => {
                        // Dropping a JoinHandle detaches the task. In particular,
                        // NEVER cancel CC Switch halfway through a token refresh:
                        // it must retain responsibility for persisting rotation.
                        (None, "query_timeout")
                    }
                };
                records.push(record(&local, workspace.as_deref(), &login, quota.as_ref(), code));
            }
            if regular_file(&marker) {
                let document = json!({"schema_version":1,"source":SOURCE,
                    "generated_at":chrono::Utc::now().timestamp(),"accounts":records});
                // Diagnostics intentionally omit I/O errors, response bodies and
                // account values. No credential data is exported even on failure.
                if write_private(&directory.join(EXPORT_FILE), &document).is_err() {
                    log::debug!("VibeCue quota snapshot could not be written");
                }
            }
        }
    });
}

fn regular_file(path: &Path) -> bool {
    std::fs::symlink_metadata(path).map(|value| value.file_type().is_file()).unwrap_or(false)
}

fn record(local: &str, workspace: Option<&str>, login: &str, quota: Option<&SubscriptionQuota>, error: &str) -> Value {
    let now = chrono::Utc::now().timestamp();
    let email = if login.len() <= 254 && login.contains('@') && !login.chars().any(char::is_control) { login } else { "" };
    let mut value = json!({"ccswitch_account_id":local,"account_id":workspace,
        "email":email,"success":false,"observed_at":now,"tiers":[],"error_code":error});
    if workspace.is_none() { value["error_code"] = json!("identity_unavailable");return value; }
    let Some(quota) = quota else { return value; };
    if !quota.success {
        value["error_code"] = json!(match quota.credential_status {
            crate::services::subscription::CredentialStatus::Expired |
            crate::services::subscription::CredentialStatus::NotFound |
            crate::services::subscription::CredentialStatus::ParseError => "authorization_unavailable",
            _ => "query_failed",
        });
        return value;
    }
    // The existing query timestamps the actual HTTP result in milliseconds.
    // Preserve it rather than making an earlier result fresh at export time.
    let Some(observed) = quota.queried_at.filter(|time| *time > 0) else {
        value["error_code"] = json!("query_failed");return value;
    };
    value["observed_at"] = json!(observed / 1000);
    let tiers: Vec<Value> = quota.tiers.iter().take(32)
        .filter(|tier| tier.utilization.is_finite() && tier.utilization >= 0.0)
        .map(|tier| json!({"name":tier.name,"utilization":tier.utilization,"resets_at":tier.resets_at})).collect();
    if tiers.is_empty() { value["error_code"] = json!("quota_unavailable");return value; }
    value["success"] = json!(true);value["tiers"] = json!(tiers);value["error_code"] = Value::Null;
    value
}

fn write_private(path: &Path, value: &Value) -> std::io::Result<()> {
    let temporary = path.with_extension(format!("{}-{}.tmp",std::process::id(),chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)));
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)] {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let result = (|| {
        let mut file = options.open(&temporary)?;
        file.write_all(&serde_json::to_vec(value)?)?;
        file.sync_all()?;
        std::fs::rename(&temporary,path)
    })();
    if result.is_err() { let _ = std::fs::remove_file(&temporary); }
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::services::subscription::CredentialStatus;
    fn quota() -> SubscriptionQuota {
        let mut value = SubscriptionQuota::not_found("codex_oauth");
        value.credential_status = CredentialStatus::Valid;
        value.credential_message = Some("private upstream text".into());
        value.success = true;
        value.tiers = vec![serde_json::from_value(json!({"name":"seven_day","utilization":22.0,"resetsAt":null})).unwrap()];
        value.error = Some("secret response".into());
        value.queried_at = Some(1000000);
        value
    }
    #[test] fn exports_only_identity_and_quota_whitelist() {
        let value=record("local-one",Some("workspace-one"),"one@example.com",Some(&quota()),"");
        assert_eq!(value["success"],true);
        assert_eq!(value["tiers"][0]["utilization"],22.0);
        assert_eq!(value["observed_at"],1000);
        assert!(!value.to_string().contains("private upstream"));
        assert!(!value.to_string().contains("secret response"));
        assert!(!value.to_string().contains("token"));
    }
    #[test] fn failed_or_timed_out_queries_never_reuse_old_tiers() {
        let mut q=quota();q.success=false;q.credential_status=CredentialStatus::Expired;
        let failed=record("local-one",Some("workspace-one"),"one@example.com",Some(&q),"");
        assert_eq!(failed["success"],false);assert_eq!(failed["tiers"],json!([]));
        assert_eq!(failed["error_code"],"authorization_unavailable");
        let timeout=record("local-one",Some("workspace-one"),"one@example.com",None,"query_timeout");
        assert_eq!(timeout["success"],false);assert_eq!(timeout["tiers"],json!([]));
    }
    #[test] fn missing_workspace_does_not_export_unattributable_success() {
        let value=record("local-one",None,"one@example.com",Some(&quota()),"");
        assert_eq!(value["success"],false);assert_eq!(value["error_code"],"identity_unavailable");
    }
    #[test] fn writes_a_private_atomic_snapshot() {
        let root=std::env::temp_dir().join(format!("vibecue-quota-{}-{}",std::process::id(),chrono::Utc::now().timestamp_nanos_opt().unwrap_or(0)));
        std::fs::create_dir(&root).unwrap();
        let path=root.join(EXPORT_FILE);write_private(&path,&json!({"accounts":[]})).unwrap();
        assert_eq!(std::fs::read_dir(&root).unwrap().count(),1);
        #[cfg(unix)] {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,0o600);
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}
