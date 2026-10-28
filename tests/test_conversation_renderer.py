"""Offline Markdown/security checks with Node; no browser or network needed."""
import hashlib
from html import unescape
import json
from pathlib import Path
import shutil
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'assets/Conversation'


class ConversationRendererTests(unittest.TestCase):
    def run_renderer(self, text):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node is unavailable; native WebKit regression covers rendering')
        script = 'const m=require(process.argv[1]); const fs=require("fs"); console.log(JSON.stringify({html:m.render(fs.readFileSync(0,"utf8"))}));'
        completed = subprocess.run([node, '-e', script, str(ASSETS / 'markdown.js')],
                                   input=text, capture_output=True, text=True, check=True, timeout=15)
        return json.loads(completed.stdout)['html']

    def test_markdown_headings_lists_quotes_tables_and_inline_code(self):
        rendered = self.run_renderer('# 标题\n\n**加粗**与 `code`\n\n- 第一项\n- 第二项\n\n> 引用\n\n| 项目 | 状态 |\n|---|---|\n| 代码 | 完成 |\n\n---')
        for fragment in ['<h1>标题</h1>', '<strong>加粗</strong>', '<code>code</code>', '<ul>', '<blockquote>', '<table>', '<th>项目</th>', '<td>完成</td>', '<hr>']:
            self.assertIn(fragment, rendered)
        aligned = self.run_renderer('| left | center | right |\n| :--- | :---: | ---: |\n| a | b | c |')
        self.assertNotIn(' style=', aligned)
        for alignment in ['align-left', 'align-center', 'align-right']:
            self.assertIn(alignment, aligned)

    def test_code_blocks_keep_exact_text_and_offer_copy_control(self):
        rendered = self.run_renderer('```swift\nlet value = "<安全>"\n```')
        self.assertIn('data-action="copy-code"', rendered)
        self.assertIn('class="hljs language-swift"', rendered)
        self.assertIn('class="hljs-keyword"', rendered)
        code = re.search(r'<pre><code[^>]*>(.*?)</code></pre>', rendered, re.S).group(1)
        self.assertEqual(unescape(re.sub(r'</?span[^>]*>', '', code)), 'let value = "<安全>"\n')
        self.assertNotIn('<安全>', rendered)

    def test_highlighting_is_explicit_bounded_and_never_auto_detects(self):
        for language, text in [('', 'const value = "<safe>";'), ('unlisted-language', 'const value = "<safe>";'), ('js', 'x' * 16385)]:
            rendered = self.run_renderer('```' + language + '\n' + text + '\n```')
            self.assertNotIn('class="hljs', rendered)
            code = re.search(r'<pre><code[^>]*>(.*?)</code></pre>', rendered, re.S).group(1)
            self.assertEqual(unescape(code), text + '\n')
        adapter = (ASSETS / 'markdown.js').read_text()
        self.assertNotIn('highlightAuto', adapter)
        self.assertNotIn('highlightElement', adapter)
        rendered = self.run_renderer('```xml\n<img src=x onerror="alert(1)"><script>window.pwned=1</script>\n```')
        self.assertNotIn('<img', rendered)
        self.assertNotIn('<script', rendered)

    def test_raw_html_script_and_attribute_payloads_are_inert(self):
        rendered = self.run_renderer('<script>window.pwned=1</script>\n\n<img src=x onerror=alert(1)>\n\n```\" onclick=alert(1)\nnot code injection\n```')
        self.assertNotIn('<script', rendered)
        self.assertNotIn('<img', rendered)
        self.assertNotIn(' onclick=', rendered)
        self.assertIn('&lt;script&gt;', rendered)

    def test_remote_and_local_images_never_get_a_fetching_element(self):
        rendered = self.run_renderer('![网络图](https://example.com/private.png)\n\n![本地图](/Users/person/secret.png)')
        self.assertNotIn('<img', rendered)
        self.assertNotIn(' src=', rendered)
        self.assertNotIn('https://example.com/private.png', rendered)
        self.assertNotIn('/Users/person/secret.png', rendered)
        self.assertEqual(rendered.count('class="image-placeholder"'), 2)

    def test_links_are_native_only_and_unsafe_protocols_never_activate(self):
        rendered = self.run_renderer('[文档](https://example.com/doc) [源码](/private/tmp/source.swift:12) [相对路径](Sources/View.swift) [错误](javascript:alert%281%29) [凭据](https://name:secret@example.com/)')
        self.assertIn('data-url="https://example.com/doc"', rendered)
        self.assertIn('data-url="/private/tmp/source.swift:12"', rendered)
        self.assertIn('data-url="Sources/View.swift"', rendered)
        self.assertNotIn(' href=', rendered)
        self.assertNotIn('data-url="javascript:', rendered)
        self.assertNotIn('data-url="https://name:', rendered)

    def test_page_is_offline_without_inline_execution_or_network_styles(self):
        html = (ASSETS / 'index.html').read_text()
        self.assertIn("default-src 'none'", html)
        self.assertIn("script-src 'self'", html)
        self.assertIn("img-src 'none'", html)
        self.assertIn("connect-src 'none'", html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('https://', html)
        self.assertNotIn('@import', (ASSETS / 'transcript.css').read_text())

    def test_vendor_parser_is_pinned_and_license_is_included(self):
        parser = ASSETS / 'vendor/markdown-it.min.js'
        self.assertEqual(hashlib.sha256(parser.read_bytes()).hexdigest(), 'b06c85174d2b1bb6339dfa1f0bc938cea57f62663f132a77abbf651562542443')
        self.assertIn('Permission is hereby granted, free of charge', (ASSETS / 'vendor/markdown-it.LICENSE').read_text())
        self.assertIn('markdown-it 15.0.3', (ASSETS / 'vendor/README.md').read_text())
        self.assertEqual(hashlib.sha256((ASSETS / 'vendor/highlight.min.js').read_bytes()).hexdigest(), 'c4a399dd6f488bc97a3546e3476747b3e714c99c57b9473154c6fb8d259b9381')
        self.assertEqual(hashlib.sha256((ASSETS / 'vendor/highlight.LICENSE').read_bytes()).hexdigest(), '6c081431591d9df696c82dc598fe1423765b8a299b200ed00b281afd0f64c490')
        self.assertIn('BSD 3-Clause License', (ASSETS / 'vendor/highlight.LICENSE').read_text())


if __name__ == '__main__':
    unittest.main()
