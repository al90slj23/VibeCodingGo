# Markdown parser

`markdown-it.min.js` is the UMD browser distribution of **markdown-it 15.0.3**,
downloaded from the official npm package `markdown-it@15.0.3`. The optional source
map comment is removed so the installed offline view does not request a map.
No other parser code is changed. The MIT license is included verbatim in
`markdown-it.LICENSE`.

- Package: https://www.npmjs.com/package/markdown-it/v/15.0.3
- Source tag: https://github.com/markdown-it/markdown-it/tree/15.0.3
- Security guidance: https://github.com/markdown-it/markdown-it/blob/15.0.3/docs/safety.md
- Packaged parser SHA-256: `b06c85174d2b1bb6339dfa1f0bc938cea57f62663f132a77abbf651562542443`
- License SHA-256: `792c48c5a849a15fdf9e37e8bcf9e6d1dd13b32b46c642a748a0a46a9919d473`

VibeCue disables raw HTML, replaces image rendering with non-loading labels, and
gives links no navigable `href`. Copying and opening a user-clicked link are sent
to the native host for validation. No network loader, remote font or plugin is
used by this view.

# Code highlighting

`highlight.min.js` is the unchanged common-language browser bundle of
**highlight.js 11.11.1**, from the official npm package
`@highlightjs/cdn-assets@11.11.1`. Its **BSD-3-Clause** license is included
verbatim as `highlight.LICENSE` (highlight.js is not MIT-licensed).

- Package: https://www.npmjs.com/package/@highlightjs/cdn-assets/v/11.11.1
- Official distribution: https://github.com/highlightjs/cdn-release/tree/11.11.1
- License source: https://github.com/highlightjs/highlight.js/blob/11.11.1/LICENSE
- Packaged highlighter SHA-256: `c4a399dd6f488bc97a3546e3476747b3e714c99c57b9473154c6fb8d259b9381`
- License SHA-256: `6c081431591d9df696c82dc598fe1423765b8a299b200ed00b281afd0f64c490`

VibeCue only highlights a fenced block with an explicit, bundled language and
at most 16,384 characters. Unknown languages, unlabelled fences and longer
blocks stay escaped plain text. It never invokes automatic language detection,
DOM highlighters or network language loaders. Local CSS supplies both palettes;
copying uses the exact rendered `textContent`, including trailing newlines.
