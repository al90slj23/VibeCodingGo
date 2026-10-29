from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vibecue import ccswitch_patch as adapter


FILES = {
    adapter.COMMAND: '''use std::sync::Arc;
pub struct CodexOAuthState(pub Arc<CodexOAuthManager>);
async fn query_codex_oauth_quota_for(
    manager: &CodexOAuthManager,
    id: &str,
) -> Result<SubscriptionQuota, String> {
    manager.original_auth_and_query(id).await
}
''',
    adapter.COMMANDS: 'mod codex_oauth;\npub use codex_oauth::*;\n',
    adapter.SERVICES: 'pub mod subscription;\npub mod usage_cache;\n',
    adapter.LIB: '''mod services;
fn run() {
    Builder::default().setup(|app| {
        {
            use commands::CodexOAuthState;
            let codex_oauth_manager =
                app.state::<AppState>().codex_oauth_manager.clone();
            app.manage(CodexOAuthState(codex_oauth_manager));
        }
        Ok(())
    });
}
''',
    adapter.MANAGER: '''pub struct CodexOAuthManager { protected_refresh_state: String }
impl CodexOAuthManager {
    pub async fn list_accounts(&self) -> Vec<GitHubAccount> { vec![] }
    pub async fn chatgpt_account_id_for_account(
        &self, id: &str,
    ) -> Result<String, CodexOAuthError> { Ok(id.into()) }
    fn private_refresh_and_persist(&self) { keep_original_refresh_locks(); }
}
''',
    adapter.ACCOUNTS: 'pub struct GitHubAccount { pub id: String, pub login: String, pub other: Option<String> }',
    adapter.QUOTA: '''pub enum CredentialStatus { Valid, Expired, NotFound, ParseError }
pub struct SubscriptionQuota {
    pub credential_status: CredentialStatus,
    pub success: bool,
    pub tiers: Vec<QuotaTier>,
    pub queried_at: Option<i64>,
    pub future_unrelated_field: Option<String>,
}
pub struct QuotaTier {
    pub name: String,
    pub utilization: f64,
    pub resets_at: Option<String>,
}
''',
    adapter.CONFIG: 'pub fn get_app_config_dir() -> PathBuf { user_config_path() }',
}
TEMPLATE = b'// fixed, trusted VibeCue bridge module\npub(crate) fn start() {}\n'


class SourceAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name, value in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def prepare(self):
        return adapter.prepare_patch(self.root, TEMPLATE)

    def write(self, name, value):
        (self.root / name).write_text(value)

    def test_prepare_is_readonly_and_minimal(self):
        before = self.snapshot()
        plan = self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({change.path for change in plan.changes},
                         {adapter.COMMAND, adapter.SERVICES, adapter.LIB, adapter.MODULE})
        manifest = plan.manifest()
        self.assertEqual(len(manifest['source_hashes']), len(FILES) + 1)
        self.assertFalse(manifest['already_patched'])

    def test_apply_is_idempotent_and_never_modifies_auth_or_query_body(self):
        original_manager = (self.root / adapter.MANAGER).read_bytes()
        original_query = FILES[adapter.COMMAND].split('    manager.original_auth_and_query')[1]
        adapter.apply_patch_plan(self.prepare())
        self.assertEqual((self.root / adapter.MANAGER).read_bytes(), original_manager)
        self.assertEqual((self.root / adapter.COMMAND).read_text().split('    manager.original_auth_and_query')[1], original_query)
        self.assertEqual((self.root / adapter.MODULE).read_bytes(), TEMPLATE)
        again = adapter.adapt_source(self.root, TEMPLATE)
        self.assertTrue(again['already_patched'])
        self.assertEqual(again['changed_files'], [])
        self.assertEqual((self.root / adapter.LIB).read_text().count('services::vibecue_quota::start'), 1)

    def test_formatting_comments_strings_and_renamed_local_bindings_are_compatible(self):
        lib = FILES[adapter.LIB].replace('codex_oauth_manager =', 'quota_owner =').replace('CodexOAuthState(codex_oauth_manager)', 'CodexOAuthState(quota_owner)')
        lib = lib.replace('|app|', '|desktop|').replace('app.', 'desktop.')
        lib = lib.replace('desktop.state::<AppState>()', 'desktop /* state */ . state :: < AppState > ()')
        lib = lib.replace('desktop.manage(', 'desktop . manage (')
        lib = '// .setup(|fake| { app.manage(CodexOAuthState(fake)); })\n' + lib
        self.write(adapter.LIB, lib)
        self.write(adapter.COMMAND, FILES[adapter.COMMAND] + '\nconst FAKE: &str = r###"async fn query_codex_oauth_quota_for() { }"###;\n')
        adapter.apply_patch_plan(self.prepare())
        patched = (self.root / adapter.LIB).read_text()
        self.assertIn('desktop.manage(CodexOAuthState(quota_owner.clone()));', patched)
        self.assertIn('services::vibecue_quota::start(quota_owner);', patched)

    def test_unknown_query_signature_or_visibility_fails_without_partial_changes(self):
        for value in (FILES[adapter.COMMAND].replace('id: &str', 'id: Option<String>'),
                      FILES[adapter.COMMAND].replace('async fn query', 'pub(super) async fn query')):
            with self.subTest(value=value):
                self.write(adapter.COMMAND, value)
                before = self.snapshot()
                with self.assertRaises(adapter.PatchError) as error:self.prepare()
                self.assertEqual(error.exception.code, 'interface_changed')
                self.assertEqual(self.snapshot(), before)

    def test_duplicate_query_and_setup_anchors_fail_closed(self):
        self.write(adapter.COMMAND, FILES[adapter.COMMAND] + FILES[adapter.COMMAND])
        before = self.snapshot()
        with self.assertRaises(adapter.PatchError):self.prepare()
        self.assertEqual(self.snapshot(), before)
        self.write(adapter.COMMAND, FILES[adapter.COMMAND])
        self.write(adapter.LIB, FILES[adapter.LIB] + FILES[adapter.LIB])
        with self.assertRaises(adapter.PatchError) as error:self.prepare()
        self.assertEqual(error.exception.code, 'interface_changed')

    def test_quota_or_manager_interface_changes_are_rejected(self):
        for name, changed in ((adapter.QUOTA, FILES[adapter.QUOTA].replace('queried_at: Option<i64>', 'queried_at: String')),
                              (adapter.MANAGER, FILES[adapter.MANAGER].replace('Vec<GitHubAccount>', 'Vec<OtherAccount>'))):
            with self.subTest(name=name):
                self.write(name, changed)
                with self.assertRaises(adapter.PatchError) as error:self.prepare()
                self.assertEqual(error.exception.code, 'interface_changed')
                self.write(name, FILES[name])

    def test_foreign_module_is_never_overwritten(self):
        self.write(adapter.MODULE, '// unexpected shared implementation\n')
        before = self.snapshot()
        with self.assertRaises(adapter.PatchError) as error:self.prepare()
        self.assertEqual(error.exception.code, 'module_conflict')
        self.assertEqual(self.snapshot(), before)

    def test_foreign_start_call_or_other_manager_rejected(self):
        for extra in ('services::vibecue_quota::start(other_manager);',
                      'services::vibecue_quota::start(codex_oauth_manager);\nservices::vibecue_quota::start(codex_oauth_manager);'):
            self.write(adapter.LIB, FILES[adapter.LIB].replace('        Ok(())', extra + '\n        Ok(())'))
            before = self.snapshot()
            with self.assertRaises(adapter.PatchError) as error:self.prepare()
            self.assertEqual(error.exception.code, 'module_conflict')
            self.assertEqual(self.snapshot(), before)

    def test_known_partial_integration_can_be_completed(self):
        self.write(adapter.MODULE, TEMPLATE.decode())
        self.write(adapter.SERVICES, FILES[adapter.SERVICES] + 'pub(crate) mod vibecue_quota;\n')
        result = adapter.apply_patch_plan(self.prepare())
        self.assertEqual(set(result['changed_files']), {adapter.COMMAND, adapter.LIB})

    def test_any_source_drift_stops_the_plan_before_first_write(self):
        plan = self.prepare()
        self.write(adapter.MANAGER, FILES[adapter.MANAGER] + '\n// external change\n')
        before = self.snapshot()
        with self.assertRaises(adapter.PatchError) as error:adapter.apply_patch_plan(plan)
        self.assertEqual(error.exception.code, 'source_changed')
        self.assertEqual(self.snapshot(), before)

    def test_mid_transaction_io_failure_restores_prior_files(self):
        before = self.snapshot()
        plan = self.prepare()
        original = adapter._atomic_write
        calls = 0
        def fail_once(path, data):
            nonlocal calls
            calls += 1
            if calls == 2:raise OSError('injected failure')
            return original(path, data)
        with patch.object(adapter, '_atomic_write', side_effect=fail_once):
            with self.assertRaises(adapter.PatchError) as error:adapter.apply_patch_plan(plan)
        self.assertEqual(error.exception.code, 'write_failed')
        self.assertEqual(self.snapshot(), before)

    def test_symlink_source_is_rejected(self):
        path = self.root / adapter.COMMAND
        original = path.with_suffix('.original')
        path.rename(original)
        path.symlink_to(original.name)
        with self.assertRaises(adapter.PatchError) as error:self.prepare()
        self.assertEqual(error.exception.code, 'source_unavailable')


if __name__ == '__main__':
    unittest.main()
