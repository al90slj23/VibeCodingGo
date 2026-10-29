/* Original VibeCoding Go rendering adapter. Markdown parsing is provided by the
 * separately licensed, pinned markdown-it distribution in vendor/. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./vendor/markdown-it.min.js'), require('./vendor/highlight.min.js'));
  else root.VibeCueMarkdown = factory(root.markdownit, root.hljs);
})(typeof globalThis === 'object' ? globalThis : this, function (markdownit, highlighter) {
  'use strict';
  const escape = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
  const copyIcon = '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="7" y="7" width="9" height="10" rx="2"/><path d="M12 7V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2h2"/></svg>';

  function safeURL(value) {
    if (typeof value !== 'string') return null;
    const url = value.trim();
    if (!url || /[\u0000-\u001f\u007f]/.test(url) || url.length > 8192) return null;
    if (url[0] === '/' && url[1] !== '/' && !url.includes('\\')) return url;
    if (!url.includes('\\') && !url.includes('://') && !/^[A-Za-z][A-Za-z0-9+.-]*:/.test(url) &&
        (/^(?:\.\.?\/)/.test(url) || /^[^?#:]+\.[A-Za-z0-9]{1,12}(?::\d+)?$/.test(url))) return url;
    if (/^(https?:|mailto:|codex:)/i.test(url)) {
      try {
        const parsed = new URL(url);
        if ((parsed.protocol === 'https:' || parsed.protocol === 'http:') && (!parsed.hostname || parsed.username || parsed.password)) return null;
        return url;
      } catch (_) { return null; }
    }
    if (/^#[A-Za-z0-9_-]+$/.test(url)) return url;
    return null;
  }

  const md = markdownit({ html: false, breaks: false, linkify: true, typographer: false, maxNesting: 32 });
  // Explicit paths and Codex deep links are delegated to native validation.
  // Nothing in rendered content gets a navigable href or an image source.
  md.validateLink = value => safeURL(value) !== null;
  md.renderer.rules.link_open = function (tokens, index) {
    const url = safeURL(tokens[index].attrGet('href'));
    const title = tokens[index].attrGet('title');
    return url ? '<a class="message-link" role="link" tabindex="0" data-url="' + escape(url) + '"' + (title ? ' title="' + escape(title) + '"' : '') + '>' : '<a aria-disabled="true">';
  };
  md.renderer.rules.image = function (tokens, index) {
    const token = tokens[index];
    const alt = token.content || '图片';
    return '<span class="image-placeholder" title="图片不会在此窗口自动加载">' + escape(alt) + '<span class="image-placeholder-note"> · 图片</span></span>';
  };
  md.renderer.rules.fence = function (tokens, index) {
    const token = tokens[index], candidate = String(token.info || '').trim().split(/\s+/)[0];
    const language = /^[A-Za-z0-9_+#.-]{1,40}$/.test(candidate) ? candidate : '';
    let code = escape(token.content), highlighted = false;
    // Only explicit, locally bundled grammars are eligible. Bounded snippets
    // avoid expensive parsing; there is no guessing, loader or DOM execution.
    if (highlighter && language && token.content.length <= 16384 && highlighter.getLanguage(language)) {
      try {
        code = highlighter.highlight(token.content, { language, ignoreIllegals: true }).value;
        highlighted = true;
      } catch (_) { /* Escaped plain text remains the safe fallback. */ }
    }
    const classes = (highlighted ? 'hljs ' : '') + (language ? 'language-' + escape(language) : '');
    return '<div class="code-block"><div class="code-header"><span class="code-language">' + escape(language || '代码') + '</span><button class="code-copy" type="button" data-action="copy-code" aria-label="复制代码">' + copyIcon + '<span>复制</span></button></div><pre><code' + (classes ? ' class="' + classes + '"' : '') + '>' + code + '</code></pre></div>\n';
  };
  const originalTableOpen = md.renderer.rules.table_open || (() => '<table>\n');
  md.renderer.rules.table_open = (tokens, index, options, env, renderer) => '<div class="table-scroll" role="region" aria-label="表格" tabindex="0">' + originalTableOpen(tokens, index, options, env, renderer);
  md.renderer.rules.table_close = () => '</table></div>\n';
  for (const tag of ['th_open', 'td_open']) {
    md.renderer.rules[tag] = (tokens, index, options, env, renderer) => {
      const token = tokens[index], style = token.attrGet('style');
      const alignment = typeof style === 'string' && style.match(/^text-align:(left|center|right)$/);
      const styleIndex = token.attrIndex('style');
      if (styleIndex >= 0) token.attrs.splice(styleIndex, 1);
      if (alignment) token.attrJoin('class', 'align-' + alignment[1]);
      return renderer.renderToken(tokens, index, options);
    };
  }

  function render(text) {
    if (typeof text !== 'string') return '';
    // Keep the renderer bounded independently of the native transcript reader.
    return md.render(text.slice(0, 131072));
  }
  return Object.freeze({ render, safeURL, escape, copyIcon });
});
