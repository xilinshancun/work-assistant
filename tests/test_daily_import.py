"""Entirely synthetic local bundles and injected HTTP protocol fixtures."""
import html
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote
from unittest.mock import patch

from work_assistant import daily_import
from work_assistant.daily_import import import_daily
from work_assistant.feishu import AmbiguousError, preview, publish, reconcile
from work_assistant.reports import generate_report, load_report
from work_assistant.util import digest, dumps
from test_runtime import Base


PERIOD = "2026-01-15"
HTML = """<!doctype html><html><head><title>忽略 head</title><script>head-active</script></head>
<body><header><h1>合成项目 · 每日进展</h1><p>2026-01-15</p>
<p>区间：2026-01-14 17:30 至 2026-01-15 17:30</p></header><main>
<section id="sync-summary"><h2>今日摘要</h2><p>完成合成检查，<strong>结论仍有限</strong>。</p></section>
<section><h2>详细结果</h2><p>合成样本十个，尚未独立复现。&lt;unsafe&gt;</p>
<ul><li>保留第一条中文详情。</li><li>保留第二条限定：仅适用测试环境。</li></ul>
<details><summary>展开补充</summary><p>折叠的完整细节也须保留。</p></details>
<script>body-active</script><style>style-active</style><iframe>frame-active</iframe>
<p hidden>hidden-active</p><p style="display:none">display-active</p>
<a href="javascript:alert(1)" onclick="evil()">链接文字</a>
</section></main><footer><nav>footer-navigation</nav></footer><nav>main-navigation</nav></body></html>"""


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.fail_card = False

    def __call__(self, method, url, body=None, headers=None):
        self.calls.append((method, url, body))
        if url.endswith("/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "fixture-token"}
        if url.endswith("/docx/v1/documents"):
            data = {"document": {"document_id": "fixture-doc"}}
        elif url.endswith("/children"):
            data = {"children": [{"block_id": "fixture-block-%s" % len(self.calls)}]}
        elif "/im/v1/messages?" in url:
            if self.fail_card:
                self.fail_card = False
                raise AmbiguousError("synthetic ambiguous send")
            data = {"message_id": "fixture-message"}
        else:
            data = {"content": "fixture content"}
        return {"code": 0, "data": data}


class DailyImportTests(Base):
    def setUp(self):
        super().setUp()
        self.bundle = self.root / "synthetic-bundle"
        (self.bundle / "daily").mkdir(parents=True)
        (self.bundle / "evidence").mkdir()
        self.report_path = self.bundle / "daily" / (PERIOD + ".html")
        self.evidence_path = self.bundle / "evidence" / ("daily-" + PERIOD + ".json")
        self.state_path = self.bundle / "state.json"
        self.evidence = {"report_type": "daily", "execution_date": PERIOD, "timezone": "Asia/Shanghai",
            "covered_from": "2026-01-14T17:30:00+08:00", "covered_until": "2026-01-15T17:30:00+08:00",
            "validation": {"status": "passed", "report_sha256": ""},
            "items": [{"id": "i1", "claim": "十个合成样本；仅为源陈述", "source_ids": ["s1"],
                       "private_extra": "private-item-metadata", "grade": "measured"}],
            "sources": [{"id": "s1", "summary": "合成日志摘要，包含格式检查结果",
                         "verified_at": "2026-01-15T17:00:00+08:00", "extra": "private-source-metadata"}],
            "limitations": ["尚未独立复现，仅适用合成测试。"], "extra": {"opaque": "private-top-metadata"}}
        self.state = {"timezone": "Asia/Shanghai", "daily": {"last_successful_execution_date": PERIOD,
            "covered_until": self.evidence["covered_until"], "last_report": "daily/" + PERIOD + ".html",
            "last_evidence": "evidence/daily-" + PERIOD + ".json"}}
        self.write_bundle()

    def write_bundle(self, html=HTML):
        self.report_path.write_bytes(html.encode("utf-8"))
        self.evidence["validation"]["report_sha256"] = digest(self.report_path.read_bytes())
        self.write_metadata()

    def write_metadata(self):
        self.state_path.write_text(dumps(self.state), encoding="utf-8")
        self.evidence_path.write_text(dumps(self.evidence), encoding="utf-8")

    def load(self, period=PERIOD, **kwargs):
        # This synthetic bundle is an explicitly verified main-only template.
        kwargs.setdefault("body_scope", "main")
        return import_daily(self.s, self.p["id"], self.bundle, period, **kwargs)

    def files(self, record):
        return {p.name: p.read_text(encoding="utf-8") for p in Path(record["data"]["path"]).iterdir()}

    def document_text(self, record):
        blocks = preview(self.s, record["id"])["document"]["children"]
        return "\n".join(daily_import.block_text(b) for b in blocks)

    def test_callout_tree_fixed_tones_preserves_native_leaves_card_and_previews(self):
        url = "https://github.com/synthetic-org/synthetic-repo/pull/1"
        deliveries = ('<section data-callout-tone="orange"><h2>交付物</h2><ul>'
                      '<li><a href="%s"><strong>PR 1</strong></a>：尚未复核。</li>' % url
                      + ''.join('<li><a href="https://github.com/synthetic-org/synthetic-repo/pull/%s">合成交付物 %s</a>，仅为源陈述。</li>'
                                % (n, n) for n in range(2, 6))
                      + '</ul></section>')
        source = HTML.replace('<main>', '<main>' + deliveries).replace(
            '<section id="sync-summary">', '<section id="sync-summary" data-callout-tone="blue">').replace(
            '结论仍有限</strong>。</p>', '结论仍有限</strong>。</p><p>仍需复核。</p><p>尚未完成。</p><p>仅限合成环境。</p>')
        self.write_bundle(source)
        report = self.load()
        files = self.files(report)
        tree = json.loads(files["document.json"])
        self.assertEqual(set(tree), {"format", "title", "blocks"})
        self.assertEqual(tree["format"], "wa.document.tree.v1")
        orange, blue = tree["blocks"][1:3]
        self.assertEqual(orange["callout"], {"background_color": 2, "border_color": 2, "emoji_id": "writing_hand"})
        self.assertEqual(blue["callout"], {"background_color": 5, "border_color": 5, "emoji_id": "bulb"})
        self.assertEqual([b["block_type"] for b in orange["child_blocks"]], [4] + [12] * 5)
        self.assertEqual([b["block_type"] for b in blue["child_blocks"]], [4] + [12] * 4)
        linked = orange["child_blocks"][1]["bullet"]["elements"][0]["text_run"]
        self.assertTrue(linked["text_element_style"]["bold"])
        self.assertEqual(unquote(linked["text_element_style"]["link"]["url"]), url)
        links = [unquote(e["text_run"]["text_element_style"]["link"]["url"])
                 for block in orange["child_blocks"][1:] for e in block["bullet"]["elements"]
                 if e["text_run"].get("text_element_style", {}).get("link")]
        self.assertEqual(links, [url[:-1] + str(n) for n in range(1, 6)])
        for tone, background, border in (("orange", "#fff3e8", "#f3a45c"), ("blue", "#eaf2ff", "#80a7ed")):
            self.assertIn("class='callout callout-%s' style='background:%s;border:1px solid %s'" %
                          (tone, background, border), files["index.html"])
        self.assertIn('<a href="' + url + '"><strong>PR 1</strong></a>', files["index.html"])
        self.assertIn("<h2 class='source-line'>交付物</h2><ul><li", files["index.html"])
        self.assertIn("- **PR 1**：尚未复核。", files["daily-sync.md"])
        self.assertEqual(self.load()["id"], report["id"])
        self.write_bundle(source.replace(' data-callout-tone="orange"', '').replace(' data-callout-tone="blue"', ''))
        flat_files = self.files(self.load())
        flat = json.loads(flat_files["document.json"])
        self.assertEqual(set(flat), {"title", "children"})
        self.assertEqual(list(daily_import.text_blocks(tree["blocks"])), flat["children"])
        for filename in ("card.json", "card-preview.html", "daily-sync.md"):
            self.assertEqual(files[filename], flat_files[filename])

    def test_callout_rejects_bad_tone_empty_nested_and_unsupported_content(self):
        fragments = [
            ('<section data-callout-tone="green"><p>限定。</p></section>', "exactly orange or blue"),
            ('<section data-callout-tone=""><p>限定。</p></section>', "exactly orange or blue"),
            ('<section data-callout-tone="Orange"><p>限定。</p></section>', "exactly orange or blue"),
            ('<div data-callout-tone="orange">限定。</div>', "requires section"),
            ('<section data-callout-tone="orange"><p hidden>不可见。</p></section>', "empty callout"),
            ('<section data-callout-tone="orange"><section data-callout-tone="blue"><p>限定。</p></section></section>', "nested callouts"),
            ('<section data-callout-tone="blue"><hr></section>', "unsupported callout child"),
            ('<section data-callout-tone="orange"><p>限定<sup>1</sup>。</p></section>', "unsupported HTML element"),
            ('<section data-callout-tone="blue"><p>' + '字' * 10001 + '</p></section>', "native block exceeds"),
        ]
        for fragment, message in fragments:
            with self.subTest(fragment=fragment):
                self.write_bundle(HTML.replace('<main>', '<main>' + fragment))
                with self.assertRaisesRegex(ValueError, message):
                    self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_callout_counts_containers_and_coverage_in_total_block_limit(self):
        def source(count):
            return ('<body><h1>合成上限</h1><main><section id="sync-summary"><h2>摘要</h2><p>仅合成。</p></section>'
                    '<section data-callout-tone="blue">' + '<p>尚未验证。</p>' * count + '</section></main></body>')
        self.write_bundle(source(996))
        document = json.loads(self.files(self.load())["document.json"])
        # 998 leaves + one container + one coverage block = 1000.
        self.assertEqual(len(list(daily_import.text_blocks(document["blocks"]))), 999)
        self.write_bundle(source(997))
        with self.assertRaisesRegex(ValueError, "at most 1000 blocks"):
            self.load()

    def test_callout_reuses_link_export_checks_and_keeps_local_link_labels(self):
        fragment = ('<section data-callout-tone="orange"><p><a href="http://localhost:8080/docs">'
                    '<strong>本地链接标签</strong></a>：仍未完成。</p></section>')
        self.write_bundle(HTML.replace('<main>', '<main>' + fragment))
        files = self.files(self.load())
        self.assertIn("本地链接标签", files["document.json"])
        self.assertNotIn("localhost", files["document.json"])
        self.assertIn("<strong>本地链接标签</strong>：仍未完成。", files["index.html"])
        for href in ("https://example.invalid/docs?path=/Users/synthetic/run.txt",
                     "https://example.invalid/docs?token=synthetic-private-secret"):
            self.write_bundle(HTML.replace('<main>', '<main>' + fragment.replace("http://localhost:8080/docs", href)))
            with self.assertRaises(ValueError):
                self.load()

    def test_callout_tree_is_rejected_by_flat_publisher_before_network_or_intent(self):
        self.write_bundle(HTML.replace('<section id="sync-summary">',
                                       '<section id="sync-summary" data-callout-tone="blue">'))
        report = self.load()
        transport = FakeHTTP()
        with self.assertRaisesRegex(ValueError, "native tree documents require timeline"):
            publish(self.s, self.conf["publisher"], report["id"], confirm=True, transport=transport)
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.s.records("publication"), [])

    def test_native_document_headings_bullets_styles_and_single_metadata_title(self):
        source = """<body><header><h1>合成源标题</h1><p>重复覆盖提示</p><nav>顶部导航</nav></header><main>
        <header><p>正文头部提示</p></header><h1>重复正文标题</h1>
        <section id="sync-summary"><h2>Sync 精简版</h2>
        <p><strong>核心进展：</strong>完成合成检查，<em>尚未独立复现</em>。</p>
        <p>风险仍在，<strong><em>不能当作已解决</em></strong>。</p><p>下一步仅为计划，没有截止承诺。</p></section>
        <section><h2>进展</h2><h3>详细结果</h3><p>原文条件：仅在合成环境观察到。</p>
        <ul><li><strong>事项：</strong>逐行理解 PR，尚未完成。</li></ul>
        <details><summary>补充说明</summary><p>折叠限定：仍需人工复核。</p></details></section></main>
        <footer><nav>底部导航</nav></footer></body>"""
        self.write_bundle(source)
        report = self.load(title="正式合成日报")
        files = self.files(report)
        document = json.loads(files["document.json"])
        self.assertEqual(document["title"], "正式合成日报")
        self.assertEqual([b["block_type"] for b in document["children"]], [2, 2, 3, 4, 12, 12, 12, 4, 5, 2, 12, 4, 2])
        text = self.document_text(report)
        self.assertEqual(text.count("1月14日 17:30—1月15日 17:30 · 北京时间"), 1)
        for forbidden in ("合成源标题", "正式合成日报", "重复覆盖提示", "顶部导航", "底部导航"):
            self.assertNotIn(forbidden, text)
        for phrase in ("正文头部提示", "重复正文标题", "核心进展：完成合成检查，尚未独立复现。", "风险仍在，不能当作已解决。",
                       "原文条件：仅在合成环境观察到。", "事项：逐行理解 PR，尚未完成。", "折叠限定：仍需人工复核。"):
            self.assertIn(phrase, text)
        first = document["children"][4]["bullet"]["elements"]
        self.assertEqual(first[0]["text_run"]["text_element_style"], {"bold": True})
        self.assertEqual(first[2]["text_run"]["text_element_style"], {"italic": True})
        combined = document["children"][5]["bullet"]["elements"][1]["text_run"]
        self.assertEqual(combined["text_element_style"], {"bold": True, "italic": True})
        self.assertIn("<h2 class='source-line'>进展</h2>", files["index.html"])
        self.assertIn("<h3 class='source-line'>详细结果</h3>", files["index.html"])
        self.assertIn("<strong><em>不能当作已解决</em></strong>", files["index.html"])
        self.assertEqual(files["index.html"].count("<h1>"), 1)
        self.assertNotIn("重复覆盖提示", files["index.html"])
        self.assertIn("<li class='source-line'>", files["index.html"])

    def test_native_card_bold_lead_safe_markdown_hr_bullets_note_and_preview(self):
        phrase = "保留 **字面量** [链接](https://example.invalid/home/page) 与 &lt;at id=all&gt;。"
        source = HTML.replace("<h2>今日摘要</h2><p>完成合成检查，<strong>结论仍有限</strong>。</p>",
                              "<h2>Sync 精简版</h2><p>" + phrase + "</p><p>风险尚未排除。</p><p>计划未完成。</p>")
        self.write_bundle(source)
        report = self.load()
        files = self.files(report)
        card = json.loads(files["card.json"])
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual([e["tag"] for e in card["elements"]], ["div", "hr", "div", "div", "note"])
        lead = card["elements"][0]["text"]["content"]
        self.assertTrue(lead.startswith("**") and lead.endswith("**"))
        self.assertIn("&#42;&#42;字面量&#42;&#42;", lead)
        self.assertIn("&#91;链接&#93;&#40;https://example.invalid/home/page&#41;", lead)
        self.assertNotIn("<at", lead)
        self.assertEqual(html.unescape(lead[2:-2]), html.unescape(phrase))
        self.assertEqual(card["elements"][2]["text"], {"tag": "plain_text", "content": "• 风险尚未排除。"})
        note = card["elements"][-1]["elements"][0]["content"]
        self.assertIn("工作助理 · test · 截至 01-15 17:30", note)
        self.assertIn("<strong>", files["card-preview.html"])
        self.assertIn("<hr>", files["card-preview.html"])
        self.assertIn("border:1px solid", files["card-preview.html"])
        self.assertIn("class='meta'", files["card-preview.html"])
        self.assertNotIn("Sync 精简版", files["card.json"])

    def test_native_export_allows_http_urls_but_checks_local_paths_and_webhooks(self):
        text = "参考 https://example.invalid/home/page 与 http://example.invalid/var/test"
        self.assertEqual(daily_import.export_data(text), text)
        for text in ("https://example.invalid/page C:/private/file", "http://example.invalid/page /tmp/private.json",
                     "https://example.invalid/webhook/synthetic-secret"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                daily_import.export_data(text)

    def test_native_requires_main_and_rejects_oversize_paragraph_without_truncation(self):
        self.write_bundle(HTML.replace("<main>", "<div>").replace("</main>", "</div>"))
        with self.assertRaisesRegex(ValueError, "one visible main"):
            self.load()
        self.write_bundle(HTML.replace("<h2>详细结果</h2>", "<p>" + "字" * 10001 + "</p>"))
        with self.assertRaisesRegex(ValueError, "native block exceeds"):
            self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_native_rejects_visible_business_content_outside_main(self):
        strict_template = HTML.replace("<p>2026-01-15</p>", "").replace(
            "<p>区间：2026-01-14 17:30 至 2026-01-15 17:30</p>", "")
        for fragment in ("<section><p>PR 尚未完成。</p></section>",
                         "<article><header><p>PR 状态仍未确认。</p></header></article>",
                         "<footer><p>业务限定：仍需复核。</p></footer>"):
            with self.subTest(fragment=fragment):
                self.write_bundle(strict_template.replace("</main>", "</main>" + fragment))
                with self.assertRaisesRegex(ValueError, "visible content outside main"):
                    self.load(body_scope="strict")
        self.assertEqual(self.s.records("report"), [])

    def test_native_shell_body_header_five_sections_and_footer(self):
        source = """<!doctype html><html><body>
        <header><h1>合成日报壳层</h1><a href="#sync-summary">头部摘要导航</a>
        <p>源头部覆盖说明</p></header>
        <main><section id="sync-summary"><h2>摘要</h2><p>合成摘要，尚未独立复现。</p></section>
        <section><h2>完成事项</h2><p>只完成合成检查。</p></section>
        <section><h2>发现</h2><p>结果仍有条件。</p></section>
        <section><h2>阻塞</h2><p>仍需人工核对。</p></section>
        <section><h2>下一步</h2><p>计划尚未完成。</p></section></main>
        <footer><p>合成页脚说明</p><a href="index.html">返回报告列表</a></footer>
        </body></html>"""
        self.write_bundle(source)
        with self.assertRaisesRegex(ValueError, "visible content outside main"):
            import_daily(self.s, self.p["id"], self.bundle, PERIOD)
        report = self.load()
        document = preview(self.s, report["id"])["document"]
        self.assertEqual(document["title"], "合成日报壳层")
        self.assertEqual(sum(b["block_type"] == 4 for b in document["children"]), 5)
        text = self.document_text(report)
        for phrase in ("合成摘要，尚未独立复现。", "只完成合成检查。", "结果仍有条件。", "仍需人工核对。", "计划尚未完成。"):
            self.assertIn(phrase, text)
        for ui in ("头部摘要导航", "源头部覆盖说明", "合成页脚说明", "返回报告列表"):
            self.assertNotIn(ui, text)
        self.assertEqual(report["data"]["import_options"], {"body_scope": "main"})
        self.assertEqual(json.loads(self.files(report)["evidence.json"])["import_options"], {"body_scope": "main"})
        self.assertEqual(self.load()["id"], report["id"])

    def test_native_header_qualification_is_rejected_by_default_not_silently_removed(self):
        source = """<body><header><h1>合成日报</h1><p>采集不完整，当前结论仍受限。</p></header>
        <main><section id="sync-summary"><h2>摘要</h2><p>仅观察到合成结果。</p></section></main></body>"""
        self.write_bundle(source)
        before = self.report_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "visible content outside main"):
            import_daily(self.s, self.p["id"], self.bundle, PERIOD)
        self.assertEqual(self.s.records("report"), [])
        report = self.load(body_scope="main")
        self.assertEqual(before, self.report_path.read_bytes())
        self.assertIn("采集不完整，当前结论仍受限。", json.loads(self.files(report)["evidence.json"])["body"])
        self.assertNotIn("采集不完整", self.document_text(report))

    def test_native_body_scope_is_recorded_hashed_and_only_explicit_main_excludes_chrome(self):
        source = """<body><header><h1>合成日报</h1><nav>导航</nav></header>
        <main><section id="sync-summary"><h2>摘要</h2><p>合成结果仍有限。</p></section></main>
        <footer><nav>证据导航</nav></footer></body>"""
        self.write_bundle(source)
        strict = self.load(body_scope="strict")
        original = self.files(strict)
        main = self.load(body_scope="main")
        self.assertNotEqual(strict["data"]["hash"], main["data"]["hash"])
        self.assertEqual(original["document.json"], self.files(main)["document.json"])
        self.assertEqual(self.files(strict), original)
        self.assertEqual(strict["data"]["import_options"], {"body_scope": "strict"})
        self.assertEqual(self.load(body_scope="main")["id"], main["id"])

    def test_native_deliverable_http_links_are_clickable_with_bold_and_safe_preview(self):
        urls = ["https://github.com/synthetic-org/synthetic-repo/pull/%s" % i for i in range(1, 5)]
        urls.append("https://github.com/synthetic-org/synthetic-repo/blob/main/rfc.md")
        items = ['<li><a href="%s"><strong>PR 1</strong></a>：待复核。</li>' % urls[0],
                 '<li><strong><a href="%s">PR 2</a></strong>：待复核。</li>' % urls[1]]
        items += ['<li><a href="%s">%s</a>：待复核。</li>' % (url, "PR " + str(i + 1) if i < 4 else "RFC")
                  for i, url in enumerate(urls[2:], start=2)]
        self.write_bundle(HTML.replace("<main>", "<main><h2>交付物</h2><ul>" + "".join(items) + "</ul>"))
        report = self.load()
        document = preview(self.s, report["id"])["document"]
        self.assertEqual(daily_import.block_text(document["children"][1]), "交付物")
        linked = []
        for block in document["children"]:
            if block["block_type"] == 12:
                linked.extend(e["text_run"] for e in block["bullet"]["elements"]
                              if e["text_run"].get("text_element_style", {}).get("link"))
        self.assertEqual([unquote(run["text_element_style"]["link"]["url"]) for run in linked], urls)
        self.assertTrue(linked[0]["text_element_style"]["bold"])
        self.assertTrue(linked[1]["text_element_style"]["bold"])
        for url in urls:
            self.assertIn('href="' + url + '"', self.files(report)["index.html"])
        self.assertIn('<a href="' + urls[0] + '"><strong>PR 1</strong></a>', self.files(report)["index.html"])

    def test_native_relative_or_unsafe_links_never_become_outbound_urls(self):
        fragment = ('<p><a href="../evidence.json">本地证据</a>；'
                    '<a href="/Users/synthetic/private.json">本地详情</a>；'
                    '<a href="javascript:alert(1)">仅保留标签</a>。</p>')
        self.write_bundle(HTML.replace("<h2>详细结果</h2>", fragment))
        report = self.load()
        document = preview(self.s, report["id"])["document"]
        text = self.document_text(report)
        self.assertIn("本地证据；本地详情；仅保留标签。", text)
        for forbidden in ("../evidence.json", "/Users/synthetic/private.json", "javascript:"):
            self.assertNotIn(forbidden, self.files(report)["document.json"])
            self.assertNotIn(forbidden, self.files(report)["index.html"])
        self.assertNotIn('"link"', dumps(document))
        for href in ("https://example.invalid/docs?path=/Users/alice/run.txt",
                     "https://example.invalid/docs?token=synthetic-private-secret",
                     "https://synthetic-user:synthetic-password@example.invalid/docs"):
            with self.subTest(href=href):
                self.write_bundle(HTML.replace("<h2>详细结果</h2>", '<p><a href="' + href + '">外部资料</a></p>'))
                with self.assertRaises(ValueError) as error:
                    self.load()
                self.assertNotIn(href, str(error.exception))

    def test_native_http_links_local_and_private_targets_keep_only_labels_without_dns(self):
        urls = ["http://localhost:8080/a", "https://api.localhost./a", "http://LOCALHOST./",
                "https://host.local/a", "https://build.internal:8443/a", "https://build:443/a",
                "https://node.local./a", "http://127.0.0.1:9000/a", "http://10.0.0.2/a",
                "http://172.16.0.1/a", "http://192.168.1.2/a", "http://169.254.169.254/a",
                "http://0.0.0.0/a", "http://224.0.0.1/a", "http://[::1]:8000/a",
                "http://[fc00::1]/a", "http://[fe80::1%25en0]/a", "http://[::ffff:127.0.0.1]/a",
                "http://[::ffff:192.168.1.1]/a", "http://127.1/a", "http://2130706433/a",
                "http://0x7f.0.0.1/a", "http://%6cocalhost/a"]
        fragment = "<ul>" + "".join('<li><a href="%s"><strong>合成标签 %s</strong></a>：未完成。</li>' % (url, i)
                                     for i, url in enumerate(urls)) + "</ul>"
        self.write_bundle(HTML.replace("<h2>详细结果</h2>", fragment))
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not be used")):
            for url in urls:
                self.assertIsNone(daily_import.http_link(url), url)
            report = self.load()
        document = self.files(report)["document.json"]
        self.assertNotIn('"link"', document)
        text = self.document_text(report)
        for i, url in enumerate(urls):
            self.assertIn("合成标签 %s：未完成。" % i, text)
            self.assertNotIn(url, document)
            self.assertNotIn(url, self.files(report)["index.html"])
        self.assertIn("<strong>合成标签 0</strong>", self.files(report)["index.html"])

    def test_native_http_links_public_hosts_ports_and_ipv6_remain_clickable_without_dns(self):
        urls = ["https://github.com/synthetic-org/synthetic-repo/pull/1",
                "https://github.com:443/synthetic-org/synthetic-repo/pull/2",
                "https://localhost.example.com/a", "https://git.corp.example/a",
                "http://8.8.8.8:80/a", "https://[2606:4700:4700::1111]:443/a"]
        with patch("socket.getaddrinfo", side_effect=AssertionError("DNS must not be used")):
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(unquote(daily_import.http_link(url)["url"]), url)

    def test_native_http_links_reject_userinfo_bad_ports_and_malformed_ipv6_without_echo(self):
        urls = ["https://synthetic-user:synthetic-password@github.com/a", "http://user@localhost/a",
                "https://github.com:invalid/a", "https://github.com:65536/a", "http://[::1]:bad/a",
                "http://[not-ipv6]/a", "http://[::1/a"]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as error:
                    daily_import.http_link(url)
                self.assertNotIn(url, str(error.exception))

    def test_native_preserves_article_header_later_h1_and_h4_to_h6(self):
        fragment = ("<article><header><p>PR 状态：尚未完成。</p></header><p>限定：未独立复现。</p></article>"
                    "<h1>后续章节</h1><h4>四级标题</h4><h5>五级标题</h5><h6>六级标题</h6>")
        self.write_bundle(HTML.replace("<h2>详细结果</h2>", fragment))
        report = self.load()
        blocks = preview(self.s, report["id"])["document"]["children"]
        by_text = {daily_import.block_text(b): b["block_type"] for b in blocks}
        self.assertEqual(by_text["PR 状态：尚未完成。"], 2)
        self.assertEqual(by_text["限定：未独立复现。"], 2)
        for text, number in (("后续章节", 3), ("四级标题", 6), ("五级标题", 7), ("六级标题", 8)):
            self.assertEqual(by_text[text], number)
        self.assertIn("<h6 class='source-line'>六级标题</h6>", self.files(report)["index.html"])
        self.assertIn("###### 六级标题", self.files(report)["daily-sync.md"])

    def test_native_standard_inline_elements_do_not_split_one_bullet(self):
        fragment = ("<ul><li>状态<mark>仍待测</mark>，截止<time>17:30</time>，"
                    "<abbr>PR</abbr>范围<label>仅合成</label>，命令<kbd>check</kbd>，"
                    "<ins>新增计划</ins>待确认。</li></ul>")
        self.write_bundle(HTML.replace("<h2>详细结果</h2>", fragment))
        report = self.load()
        expected = "状态仍待测，截止17:30，PR范围仅合成，命令check，新增计划待确认。"
        matching = [b for b in preview(self.s, report["id"])["document"]["children"]
                    if daily_import.block_text(b).startswith("状态")]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["block_type"], 12)
        self.assertEqual(daily_import.block_text(matching[0]), expected)
        runs = matching[0]["bullet"]["elements"]
        self.assertTrue(next(e["text_run"] for e in runs if e["text_run"]["content"] == "check")
                        ["text_element_style"]["inline_code"])

    def test_native_rejects_unsupported_inline_semantics_without_flattening(self):
        for tag in ("sup", "sub", "q", "custom-inline"):
            with self.subTest(tag=tag):
                fragment = "<ul><li>原文<%s>2</%s>仍待确认。</li></ul>" % (tag, tag)
                self.write_bundle(HTML.replace("<h2>详细结果</h2>", fragment))
                with self.assertRaisesRegex(ValueError, "unsupported HTML element"):
                    self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_native_table_cell_paragraphs_and_divs_have_boundaries(self):
        fragment = "<table><tr><td><p>10</p><p>20</p></td><td><div>30</div><div>40</div></td></tr></table>"
        self.write_bundle(HTML.replace("<h2>详细结果</h2>", fragment))
        text = self.document_text(self.load())
        self.assertIn("| 10; 20 | 30; 40", text)
        self.assertNotIn("1020", text)
        self.assertNotIn("3040", text)

    def test_native_strikethrough_survives_document_card_and_previews(self):
        fragment = "<h2>摘要</h2><p><s>已完成</s>，实际仍待测试。</p><p><del>风险已消除</del>，仍需复核。</p>"
        self.write_bundle(HTML.replace("<h2>今日摘要</h2><p>完成合成检查，<strong>结论仍有限</strong>。</p>", fragment))
        report = self.load()
        files = self.files(report)
        blocks = preview(self.s, report["id"])["document"]["children"]
        for text in ("已完成", "风险已消除"):
            block = next(b for b in blocks if daily_import.block_text(b).startswith(text))
            self.assertTrue(block["bullet"]["elements"][0]["text_run"]["text_element_style"]["strikethrough"])
            self.assertIn("<del>" + text + "</del>", files["index.html"])
            self.assertIn("<del>" + text + "</del>", files["card-preview.html"])
            self.assertIn("~~" + text + "~~", files["daily-sync.md"])
        card = json.loads(files["card.json"])
        self.assertIn("~~已完成~~", card["elements"][0]["text"]["content"])
        self.assertEqual(card["elements"][2]["text"]["tag"], "lark_md")
        self.assertIn("~~风险已消除~~", card["elements"][2]["text"]["content"])

    def test_native_url_query_and_fragment_cannot_hide_local_paths(self):
        normal = "https://example.invalid/home/page?next=/guides/intro#section"
        self.assertEqual(daily_import.export_data(normal), normal)
        urls = ["https://example.invalid/docs?path=/Users/alice/run.txt",
                "https://example.invalid/docs#path=/Users/alice/run.txt",
                "https://example.invalid/docs?path=%2FUsers%2Falice%2Frun.txt",
                "https://example.invalid/docs#%252FUsers%252Falice%252Frun.txt",
                "http://example.invalid/docs?path=C%3A%5Cprivate%5Crun.txt"]
        for url in urls:
            with self.subTest(url=url), self.assertRaisesRegex(ValueError, "local path"):
                daily_import.export_data(url)

    def test_native_coverage_is_natural_chinese_with_cross_year_and_other_zone(self):
        stamp = daily_import._timestamp
        self.assertEqual(daily_import.coverage_text(stamp("2026-10-28T17:30:15+08:00", "start"),
                         stamp("2026-10-29T17:30:42+08:00", "end"), "Asia/Shanghai"),
                         "10月28日 17:30—10月29日 17:30 · 北京时间")
        self.assertEqual(daily_import.coverage_text(stamp("2025-12-31T17:30:00Z", "start"),
                         stamp("2026-01-01T17:30:00Z", "end"), "UTC"),
                         "2025年12月31日 17:30—2026年1月1日 17:30 · UTC")
        self.evidence["covered_from"] = "2026-01-14T17:30:15+08:00"
        self.evidence["covered_until"] = "2026-01-15T17:30:42+08:00"
        self.state["daily"]["covered_until"] = self.evidence["covered_until"]
        self.write_metadata()
        report = self.load()
        first = preview(self.s, report["id"])["document"]["children"][0]
        self.assertEqual(daily_import.block_text(first), "1月14日 17:30—1月15日 17:30 · 北京时间")
        self.assertEqual(report["data"]["coverage"]["covered_until"], "2026-01-15T17:30:42+08:00")

    def test_date_aliases_require_one_and_agree_when_both_present(self):
        for fields in ({"execution_date": PERIOD}, {"report_date": PERIOD},
                       {"execution_date": PERIOD, "report_date": PERIOD}):
            with self.subTest(fields=fields):
                self.evidence.pop("execution_date", None)
                self.evidence.pop("report_date", None)
                self.evidence.update(fields)
                self.write_metadata()
                before = self.evidence_path.read_bytes()
                report = self.load()
                self.assertEqual(report["data"]["source_evidence"], self.evidence)
                self.assertEqual(self.evidence_path.read_bytes(), before)
                self.assertEqual(report["data"]["period"], PERIOD)
        for fields in ({}, {"report_date": "2026-01-14"},
                       {"execution_date": PERIOD, "report_date": "2026-01-14"},
                       {"execution_date": None, "report_date": PERIOD}):
            with self.subTest(fields=fields):
                self.evidence.pop("execution_date", None)
                self.evidence.pop("report_date", None)
                self.evidence.update(fields)
                self.write_metadata()
                with self.assertRaisesRegex(ValueError, "execution_date/report_date"):
                    self.load()

    def test_report_date_source_mapping_without_items_or_source_summaries(self):
        del self.evidence["execution_date"]
        del self.evidence["items"]
        self.evidence.update(schema_version=1, report_date=PERIOD,
            source_mapping={"progress": ["s_build", "s_test"], "support": ["s_scope"]},
            sources=[{"id": "s_build", "summary": "合成构建记录", "verified_at": "2026-01-15T16:00:00+08:00"},
                     {"id": "s_test", "facts": {"synthetic": True}, "limits": ["仅合成测试"],
                      "verified_at": "2026-01-15T16:00:00+08:00"},
                     {"id": "s_scope", "note": "合成范围说明", "verified_at": "2026-01-15T16:00:00+08:00"}])
        self.write_metadata()
        source_before = self.evidence_path.read_bytes()
        report = self.load(title="合成蓝色日报")
        files = self.files(report)
        snapshot = json.loads(files["evidence.json"])
        self.assertEqual(snapshot["evidence"]["items"], [])
        self.assertEqual(snapshot["claims"], [])
        self.assertEqual(snapshot["evidence"]["source_mapping"], self.evidence["source_mapping"])
        self.assertEqual(snapshot["evidence"]["source_mapping_grade"], "reported")
        self.assertIn("progress → s_build, s_test", files["evidence.html"])
        self.assertIn("源未提供摘要 / No source summary supplied", files["evidence.html"])
        self.assertEqual(snapshot["evidence"]["sources"][1]["summary"], "源未提供摘要 / No source summary supplied")
        self.assertEqual(report["data"]["source_evidence"], self.evidence)
        self.assertNotIn("execution_date", report["data"]["source_evidence"])
        self.assertNotIn("summary", report["data"]["source_evidence"]["sources"][1])
        self.assertEqual(self.evidence_path.read_bytes(), source_before)
        card = json.loads(files["card.json"])
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["header"]["title"]["content"], "合成蓝色日报")
        for forbidden in ("source_mapping", "s_build", "s_test", "s_scope", "No source summary supplied"):
            self.assertNotIn(forbidden, files["card.json"])
            self.assertNotIn(forbidden, self.document_text(report))
        self.assertEqual(self.load(title="合成蓝色日报")["id"], report["id"])
        self.assertEqual(self.s.records("claim"), [])

    def test_source_mapping_rejects_unknown_refs_empty_arrays_and_wrong_shapes(self):
        del self.evidence["items"]
        for mapping in ({"progress": ["missing"]}, {"progress": []}, {"progress": "s1"},
                        {"progress": [1]}, {"": ["s1"]}, [], None):
            with self.subTest(mapping=mapping):
                self.evidence["source_mapping"] = mapping
                self.write_metadata()
                with self.assertRaisesRegex(ValueError, "source_mapping"):
                    self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_optional_summary_is_validated_when_present_source_verified_at_optional(self):
        for summary in (None, "", "字" * 2001, 123):
            with self.subTest(summary=summary):
                self.evidence["sources"][0]["summary"] = summary
                self.write_metadata()
                with self.assertRaisesRegex(ValueError, "source summary"):
                    self.load()
        del self.evidence["sources"][0]["summary"]
        del self.evidence["sources"][0]["verified_at"]
        self.write_metadata()
        self.assertIn("源未提供验证时间 / Not supplied", self.files(self.load())["evidence.html"])

    def test_instruction_source_without_verified_at_stays_reported_and_private(self):
        instruction = {"id": "s_instruction", "post_cutoff_instruction_only": True,
                       "summary": "合成用户指令：任务梳理和逐行理解 PR，仍待完成。"}
        self.evidence.pop("items")
        self.evidence["sources"].append(instruction)
        self.evidence["source_mapping"] = {"next-actions": ["s_instruction"]}
        self.write_metadata()
        before = self.evidence_path.read_bytes()
        report = self.load()
        files = self.files(report)
        local = json.loads(files["evidence.json"])
        selected = local["evidence"]["sources"][-1]
        self.assertEqual(selected["id"], "s_instruction")
        self.assertEqual(selected["grade"], "reported")
        self.assertEqual(selected["verified_at"], "源未提供验证时间 / Not supplied")
        self.assertEqual(local["claims"], [])
        self.assertEqual(report["data"]["source_evidence"]["sources"][-1], instruction)
        self.assertNotIn("verified_at", report["data"]["source_evidence"]["sources"][-1])
        self.assertEqual(self.evidence_path.read_bytes(), before)
        self.assertIn("源未提供验证时间 / Not supplied", files["evidence.html"])
        for name in ("document.json", "card.json"):
            self.assertNotIn(instruction["summary"], files[name])
            self.assertNotIn("Not supplied", files[name])
        ordinary = generate_report(self.s, self.p["id"], PERIOD)
        ordinary_snapshot = json.loads(self.files(ordinary)["evidence.json"])
        self.assertEqual(ordinary_snapshot["claims"], [])
        self.assertEqual(ordinary_snapshot["tasks"], [])
        self.assertNotIn(instruction["summary"], self.files(ordinary)["index.html"])
        self.assertEqual(self.s.records("claim"), [])

    def test_source_verified_at_present_requires_nonempty_bounded_text(self):
        for value in (None, "", " ", 123, False, "字" * 101):
            with self.subTest(value=value):
                self.evidence["sources"][0]["verified_at"] = value
                self.write_metadata()
                with self.assertRaisesRegex(ValueError, "source verified_at"):
                    self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_mapping_can_reference_distinct_pr_observations_without_exporting_details(self):
        observation = {"id": "pr_fixture", "verified_at": "2026-01-15T17:15:00+08:00",
            "url": "https://example.invalid/pull/7", "author": "synthetic-author", "state": "open",
            "merged_at": None, "head_sha": "0123456789abcdef", "review": {"status": "fixture-review"},
            "checks": ["fixture-check"], "note": "synthetic-private-note",
            "verification_history": [{"at": "2026-01-15T17:00:00+08:00", "note": "fixture-history"}]}
        self.evidence.pop("items")
        self.evidence.update(pr_observations=[observation], source_mapping={"progress": ["s1", "pr_fixture"]})
        self.write_metadata()
        before = self.evidence_path.read_bytes()
        report = self.load()
        files = self.files(report)
        local = json.loads(files["evidence.json"])["evidence"]
        self.assertEqual(local["sources"][0]["kind"], "source")
        self.assertEqual(local["pr_observations"], [{"id": "pr_fixture", "kind": "pr_observation",
                                                   "verified_at": observation["verified_at"]}])
        self.assertNotIn("summary", local["pr_observations"][0])
        self.assertEqual(local["items"], [])
        self.assertEqual(local["source_mapping"], {"progress": ["s1", "pr_fixture"]})
        self.assertEqual(report["data"]["source_evidence"]["pr_observations"], [observation])
        self.assertIn("pr_fixture [pr_observation]", files["evidence.html"])
        self.assertEqual(before, self.evidence_path.read_bytes())
        for detail in ("pr_fixture", observation["verified_at"], observation["url"], observation["author"],
                       observation["head_sha"], "fixture-review", "fixture-check", observation["note"], "fixture-history"):
            self.assertNotIn(detail, files["document.json"])
            self.assertNotIn(detail, files["card.json"])
        self.assertEqual(self.s.records("claim"), [])

    def test_pr_observation_refs_require_ids_verified_at_and_no_collisions(self):
        valid = {"id": "pr_fixture", "verified_at": "2026-01-15T17:15:00+08:00"}
        self.evidence["source_mapping"] = {"progress": ["pr_fixture"]}
        cases = [(None, "pr_observations"), (["bad"], "must be an object"),
                 ([{"verified_at": valid["verified_at"]}], "PR observation id"),
                 ([{"id": "pr_fixture"}], "PR observation verified_at"),
                 ([dict(valid, id="s1")], "duplicate evidence source id"),
                 ([valid, dict(valid)], "duplicate evidence source id")]
        for observations, message in cases:
            with self.subTest(observations=observations):
                self.evidence["pr_observations"] = observations
                self.write_metadata()
                with self.assertRaisesRegex(ValueError, message):
                    self.load()
        self.evidence["pr_observations"] = [valid]
        self.evidence["source_mapping"] = {"progress": ["pr_missing"]}
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "source_mapping"):
            self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_card_uses_local_short_time_and_separate_unchanged_plain_text_paragraphs(self):
        paragraphs = ["进展：保留 **原文** 与 [示例](fixture-reference)。",
                      "风险：尚未独立复现，仅适用合成环境。", "下一步：人工核对，没有承诺截止日期。"]
        summary_html = "<h2>Sync 精简版</h2>" + "".join("<p>" + text + "</p>" for text in paragraphs)
        self.evidence["covered_until"] = "2026-01-15T09:30:00Z"
        self.state["daily"]["covered_until"] = self.evidence["covered_until"]
        self.write_bundle(HTML.replace("<h2>今日摘要</h2><p>完成合成检查，<strong>结论仍有限</strong>。</p>", summary_html))
        report = self.load()
        files = self.files(report)
        card = json.loads(files["card.json"])
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["elements"][0]["text"]["tag"], "lark_md")
        self.assertEqual(html.unescape(card["elements"][0]["text"]["content"])[2:-2], paragraphs[0])
        self.assertEqual(card["elements"][1], {"tag": "hr"})
        self.assertEqual([e["text"]["content"] for e in card["elements"][2:-1]], ["• " + p for p in paragraphs[1:]])
        self.assertEqual(card["elements"][-1]["tag"], "note")
        self.assertNotIn(self.evidence["covered_from"], files["card.json"])
        self.assertNotIn(self.evidence["covered_until"], files["card.json"])
        self.assertNotIn(self.evidence["covered_until"], files["card-preview.html"])
        self.assertIn("截至 01-15 17:30", files["card-preview.html"])
        snapshot = json.loads(files["evidence.json"])
        self.assertEqual(snapshot["summary"], "\n".join(["Sync 精简版"] + paragraphs))
        self.assertIn("1月14日 17:30", self.document_text(report))
        self.assertIn("1月15日 17:30", self.document_text(report))
        for paragraph in paragraphs:
            self.assertIn(paragraph, snapshot["body"])
            self.assertIn(paragraph, self.document_text(report))

    def test_long_card_paragraph_is_kept_whole_without_chunking_or_truncation(self):
        paragraph = "合成详情" * 400
        self.write_bundle(HTML.replace("<h2>今日摘要</h2><p>完成合成检查，<strong>结论仍有限</strong>。</p>",
                                       "<p>" + paragraph + "</p>"))
        card = json.loads(self.files(self.load())["card.json"])
        self.assertEqual(html.unescape(card["elements"][0]["text"]["content"])[2:-2], paragraph)

    def test_compact_table_preserves_caption_cells_and_rows(self):
        table = "<table><caption>样本计数</caption><tr><th>A<th>B<tr><td>10<td>20</table>"
        self.write_bundle(HTML.replace("<h2>详细结果</h2>", table))
        report = self.load()
        body = json.loads(self.files(report)["evidence.json"])["body"]
        self.assertIn("样本计数\n| A | B\n| 10 | 20", body)
        self.assertIn("| A | B\n| 10 | 20", self.document_text(report))
        self.assertNotIn("1020", body)

    def test_optional_hidden_paragraph_and_list_end_tags_keep_visible_qualifications(self):
        examples = ["<p hidden>不应显示<p>限定：尚未独立复现。",
                    "<ul><li hidden>不应显示<li>限定：尚未独立复现。</ul>",
                    "<p hidden>不应显示<br>仍不应显示<p>限定：尚未独立复现。"]
        for fragment in examples:
            with self.subTest(fragment=fragment):
                self.write_bundle(HTML.replace("<h2>详细结果</h2>", fragment))
                text = self.document_text(self.load())
                self.assertIn("限定：尚未独立复现。", text)
                self.assertNotIn("不应显示", text)

    def test_ambiguous_unclosed_hidden_container_is_rejected(self):
        for fragment in ("<div hidden>隐私<p>限定仍可见", "<p hidden><span>隐私<p>限定仍可见"):
            with self.subTest(fragment=fragment):
                self.write_bundle("<body><h1>测试</h1><section id='sync-summary'>摘要</section><section>"
                                  + fragment + "</section></body>")
                with self.assertRaisesRegex(ValueError, "ambiguous HTML nesting"):
                    self.load()

    def test_cutoff_local_date_must_equal_requested_historical_date(self):
        self.evidence["covered_until"] = "2026-01-15T16:30:00Z"  # Jan 16 in Asia/Shanghai.
        self.state["daily"]["covered_until"] = self.evidence["covered_until"]
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "covered_until date"):
            self.load()
        self.evidence["covered_until"] = "2026-01-14T16:30:00Z"  # Jan 15 in Asia/Shanghai.
        self.state["daily"]["covered_until"] = self.evidence["covered_until"]
        self.write_metadata()
        self.assertEqual(self.load()["data"]["period"], PERIOD)

    def test_parameter_prose_preserved_without_generic_redaction(self):
        phrase = "上下文 max token: 4096，password=至少12位"
        detail = "max_tokens=8192；token: 123456789012；password 参数名不代表密码值。"
        self.evidence["items"][0]["claim"] = phrase
        self.write_bundle(HTML.replace("今日摘要", "&apos;" + phrase + "&apos;")
                          .replace("<h2>详细结果</h2>", "<p>" + detail + "</p>"))
        report = self.load(title=phrase)
        files = self.files(report)
        snapshot = json.loads(files["evidence.json"])
        self.assertIn("'" + phrase + "'", snapshot["body"])
        self.assertIn(detail, snapshot["body"])
        self.assertIn(phrase, self.document_text(report))
        self.assertIn(detail, self.document_text(report))
        self.assertEqual(snapshot["evidence"]["items"][0]["claim"], phrase)
        self.assertIn(phrase, json.loads(files["card.json"])["header"]["title"]["content"])
        for name in ("index.html", "evidence.html", "card-preview.html", "daily-sync.md"):
            self.assertIn(phrase, files[name])

    def test_import_rejects_explicit_secrets_and_local_paths_without_echoing(self):
        sensitive = ["token=synthetic-test-credential", "password=synthetic-password-123",
                     "sk-synthetic_fixture_key_123", "Bearer synthetic_access_123456",
                     "https://example.invalid/webhook/synthetic-id", "/Users/synthetic/private/report.json",
                     "file:///tmp/synthetic-report.json", "C:\\synthetic\\private.json"]
        for text in sensitive:
            with self.subTest(text=text):
                self.write_bundle(HTML.replace("今日摘要", text))
                with self.assertRaisesRegex(ValueError, "daily export contains") as error:
                    self.load()
                self.assertNotIn(text, str(error.exception))
        self.write_bundle()
        self.evidence["sources"][0]["summary"] = "token=synthetic-private-secret"
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "credential assignment"):
            self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_pre_preserves_newlines_indentation_and_blank_lines(self):
        pre = "epoch 1\n  loss 0.20\n\n  loss 0.10"
        self.write_bundle(HTML.replace("<h2>详细结果</h2>", "<pre>" + pre + "</pre>"))
        report = self.load()
        files = self.files(report)
        self.assertIn(pre, json.loads(files["evidence.json"])["body"])
        # Document blocks preserve the blank line as a space-only native text block.
        self.assertIn("epoch 1\n  loss 0.20\n \n  loss 0.10", self.document_text(report))
        self.assertIn("<p class='source-line'>  loss 0.20</p>", files["index.html"])
        self.assertIn("white-space:pre-wrap", files["index.html"])

    def test_first_visible_h1_only_is_title_later_h1_remains_body(self):
        self.write_bundle(HTML.replace("<header><h1>", "<header><h1 hidden>隐藏标题</h1><h1>")
                          .replace("<h2>详细结果</h2>", "<h1>第二章节标题</h1>"))
        report = self.load()
        payload = preview(self.s, report["id"])
        self.assertEqual(payload["document"]["title"], "合成项目 · 每日进展")
        self.assertEqual(payload["card"]["header"]["title"]["content"], "合成项目 · 每日进展")
        self.assertIn("第二章节标题", json.loads(self.files(report)["evidence.json"])["body"])
        self.assertIn("第二章节标题", self.document_text(report))
        self.assertNotIn("隐藏标题", self.document_text(report))

    def test_identical_import_restores_missing_files_but_never_tampering(self):
        report = self.load()
        before = self.files(report)
        directory = Path(report["data"]["path"])
        (directory / "card.json").unlink()
        (directory / "document.json").unlink()
        with self.assertRaisesRegex(ValueError, "immutable report"):
            load_report(self.s, report["id"])
        self.assertEqual(self.load()["id"], report["id"])
        self.assertEqual(self.files(report), before)
        shutil.rmtree(directory)
        self.assertEqual(self.load()["id"], report["id"])
        self.assertEqual(self.files(report), before)
        (directory / "card.json").unlink()
        (directory / "document.json").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "immutable report modified"):
            self.load()
        self.assertFalse((directory / "card.json").exists())
        self.assertEqual((directory / "document.json").read_text(), "tampered")
        self.assertEqual(len(self.s.records("report")), 1)

    def test_summary_full_report_qualifications_and_private_metadata(self):
        before = {p: p.read_bytes() for p in (self.state_path, self.report_path, self.evidence_path)}
        report = self.load()
        files = self.files(report)
        self.assertEqual(len(files), 7)
        self.assertEqual(load_report(self.s, report["id"]), report)
        card = json.loads(files["card.json"])
        self.assertEqual(card["header"]["title"]["content"], "合成项目 · 每日进展")
        card_text = "\n".join(e["text"]["content"] for e in card["elements"] if e["tag"] == "div")
        self.assertIn("结论仍有限", card_text)
        self.assertIn("截至 01-15 17:30", card["elements"][-1]["elements"][0]["content"])
        self.assertNotIn("第一条中文详情", card_text)
        document = preview(self.s, report["id"])["document"]
        text = self.document_text(report)
        for expected in ("1月15日 17:30", "第一条中文详情", "第二条限定", "折叠的完整细节", "尚未独立复现"):
            self.assertIn(expected, text)
        for forbidden in (daily_import.SCOPE, "证据摘要", "十个合成样本；仅为源陈述", "s1", "verified_at", "[REDACTED]"):
            self.assertNotIn(forbidden, text)
            self.assertNotIn(forbidden, dumps(card))
        self.assertIn("十个合成样本；仅为源陈述", files["evidence.html"])
        self.assertIn("合成日志摘要", files["evidence.json"])
        self.assertIn(daily_import.SCOPE, files["evidence.html"])
        self.assertIn("&lt;unsafe&gt;", files["index.html"])
        for raw in files.values():
            for forbidden in ("head-active", "body-active", "style-active", "frame-active", "hidden-active",
                              "display-active", "footer-navigation", "main-navigation", "javascript:", "onclick",
                              "private-item-metadata", "private-source-metadata", "private-top-metadata", "synthetic-private-secret"):
                self.assertNotIn(forbidden, raw)
        self.assertEqual(report["data"]["source_evidence"], self.evidence)
        self.assertEqual(report["data"]["source_hashes"]["report"], digest(before[self.report_path]))
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertEqual(self.s.records("claim"), [])
        self.assertEqual(self.s.records("evidence"), [])
        self.assertEqual(json.loads(files["evidence.json"])["evidence"]["items"][0]["grade"], "reported")

    def test_determinism_changed_same_period_and_immutability(self):
        first = self.load()
        old = self.files(first)
        self.assertEqual(self.load()["id"], first["id"])
        self.write_bundle(HTML.replace("第一条中文详情", "修订后的中文详情"))
        second = self.load()
        self.assertNotEqual(second["id"], first["id"])
        self.assertNotEqual(second["data"]["hash"], first["data"]["hash"])
        self.assertEqual(first["data"]["period"], second["data"]["period"])
        self.assertEqual(self.files(first), old)
        self.assertEqual(self.load()["id"], second["id"])
        Path(second["data"]["path"], "index.html").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "immutable report"):
            self.load()

    def test_title_override_changes_display_hash_preserves_source_and_period(self):
        source_before = self.report_path.read_bytes()
        first = self.load()
        title = "合成日报 · 2026-01-15 提前预览"
        updated = self.load(title=title)
        payload = preview(self.s, updated["id"])
        self.assertEqual(payload["document"]["title"], title)
        self.assertEqual(payload["card"]["header"]["title"]["content"], title)
        self.assertNotIn(title, self.document_text(updated))
        self.assertEqual(daily_import.block_text(payload["document"]["children"][0]), "1月14日 17:30—1月15日 17:30 · 北京时间")
        self.assertNotEqual(first["data"]["hash"], updated["data"]["hash"])
        self.assertEqual(first["data"]["period"], updated["data"]["period"])
        self.assertEqual(first["data"]["source_hashes"], updated["data"]["source_hashes"])
        self.assertEqual(source_before, self.report_path.read_bytes())
        original_snapshot = json.loads(self.files(first)["evidence.json"])
        updated_snapshot = json.loads(self.files(updated)["evidence.json"])
        self.assertEqual(original_snapshot["body"], updated_snapshot["body"])
        self.assertEqual(updated_snapshot["source_title"], "合成项目 · 每日进展")
        self.assertEqual(updated["id"], self.load(title=title)["id"])
        for invalid in ("", " \n\t", "字" * 201):
            with self.subTest(title=invalid), self.assertRaisesRegex(ValueError, "display title"):
                self.load(title=invalid)
        self.assertEqual(preview(self.s, self.load(title="字" * 200)["id"])["document"]["title"], "字" * 200)

    def test_next_day_does_not_accumulate_old_claims(self):
        self.load()
        self.state["daily"]["last_successful_execution_date"] = "2026-01-16"
        self.evidence["execution_date"] = "2026-01-16"
        self.evidence["covered_until"] = "2026-01-16T17:30:00+08:00"
        self.state["daily"]["covered_until"] = self.evidence["covered_until"]
        self.evidence["items"][0]["claim"] = "下一日的新结论"
        self.write_bundle(HTML.replace("第一条中文详情", "次日详情"))
        record = self.load("2026-01-16")
        outgoing = json.loads(self.files(record)["evidence.json"])
        self.assertEqual([i["claim"] for i in outgoing["evidence"]["items"]], ["下一日的新结论"])

    def test_rejects_stale_timezone_type_watermark_and_validation(self):
        cases = [("state-date", "state execution date", lambda: self.state["daily"].update(last_successful_execution_date="2026-01-14")),
                 ("evidence-date", "execution_date", lambda: self.evidence.update(execution_date="2026-01-14")),
                 ("state-zone", "state timezone", lambda: self.state.update(timezone="UTC")),
                 ("evidence-zone", "evidence timezone", lambda: self.evidence.update(timezone="UTC")),
                 ("type", "must be daily", lambda: self.evidence.update(report_type="weekly")),
                 ("watermark", "watermark mismatch", lambda: self.state["daily"].update(covered_until="2026-01-14T17:30:00+08:00")),
                 ("unvalidated", "validation.status", lambda: self.evidence["validation"].update(status="failed")),
                 ("hash", "sha256 mismatch", lambda: self.evidence["validation"].update(report_sha256="0" * 64)),
                 ("order", "must precede", lambda: self.evidence.update(covered_from=self.evidence["covered_until"])),
                 ("naive", "timezone offset", lambda: self.evidence.update(covered_from="2026-01-14T17:30:00")),
                 ("future", "future cutoff", lambda: (self.evidence.update(covered_until="2999-01-15T17:30:00+08:00"), self.state["daily"].update(covered_until="2999-01-15T17:30:00+08:00")))]
        initial_state, initial_evidence = dumps(self.state), dumps(self.evidence)
        for label, message, mutate in cases:
            with self.subTest(label=label):
                self.state, self.evidence = json.loads(initial_state), json.loads(initial_evidence)
                mutate()
                self.write_metadata()
                with self.assertRaisesRegex(ValueError, message):
                    self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_traversal_and_symlink_escape_including_state(self):
        outside = self.root / "outside.html"
        outside.write_text(HTML)
        for value in ("../outside.html", str(outside)):
            self.state["daily"]["last_report"] = value
            self.write_metadata()
            with self.assertRaisesRegex(ValueError, "escapes source-root"):
                self.load()
        link = self.bundle / "escape.html"
        link.symlink_to(outside)
        self.state["daily"]["last_report"] = "escape.html"
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "escapes source-root"):
            self.load()
        state_copy = self.root / "outside-state.json"
        state_copy.write_bytes(self.state_path.read_bytes())
        self.state_path.unlink()
        self.state_path.symlink_to(state_copy)
        with self.assertRaisesRegex(ValueError, "escapes source-root"):
            self.load()

    def test_input_mutation_race_refuses_commit(self):
        original = daily_import.render_import
        for path in (self.state_path, self.report_path, self.evidence_path):
            with self.subTest(path=path.name):
                self.write_bundle()
                def mutate(snapshot):
                    result = original(snapshot)
                    path.write_bytes(path.read_bytes() + b" ")
                    return result
                with patch.object(daily_import, "render_import", mutate), self.assertRaisesRegex(ValueError, "inputs changed"):
                    self.load()
                self.assertEqual(self.s.records("report"), [])
                self.assertFalse((self.s.root / "reports").exists())

    def test_empty_duplicate_or_oversize_summary_and_missing_body(self):
        cases = [(HTML.replace('id="sync-summary"', 'id="other"'), "sync-summary"),
                 (HTML.replace("<body>", "<body><p id='sync-summary'>duplicate</p>"), "exactly one"),
                 ("<body><h1>Title</h1><section id='sync-summary'></section></body>", "sync-summary"),
                 (HTML.replace("今日摘要", "字" * 6001), "sync-summary"),
                 ("<head><h1>hidden</h1><section id='sync-summary'>hidden</section></head>", "visible body")]
        for html, message in cases:
            with self.subTest(message=message):
                self.write_bundle(html)
                with self.assertRaisesRegex(ValueError, message):
                    self.load()

    def test_file_and_document_limits_reject_without_truncation(self):
        with patch.object(daily_import, "MAX_FILE", 50), self.assertRaisesRegex(ValueError, "size limit"):
            self.load()
        detail = "<p>完整详情</p>" * 1001
        self.write_bundle(HTML.replace("<h2>详细结果</h2>", detail))
        with self.assertRaisesRegex(ValueError, "document too large"):
            self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_extra_metadata_is_not_outgoing_but_changes_identity(self):
        first = self.load()
        self.evidence["extra"]["opaque"] = "new-private-value"
        self.write_metadata()
        second = self.load()
        self.assertNotEqual(first["data"]["hash"], second["data"]["hash"])
        self.assertNotIn("new-private-value", "\n".join(self.files(second).values()))

    def test_missing_invalid_inputs_and_bounded_evidence(self):
        self.evidence_path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.load()
        self.write_bundle()
        self.state_path.write_text('{"daily":{},"daily":{}}')
        with self.assertRaisesRegex(ValueError, "valid UTF-8 JSON"):
            self.load()
        self.write_bundle()
        self.evidence["items"][0]["source_ids"] = ["missing-source"]
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "source_ids"):
            self.load()
        self.evidence["items"][0]["source_ids"] = ["s1"]
        self.evidence["sources"][0]["summary"] = "字" * 2001
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "source summary"):
            self.load()
        self.assertEqual(self.s.records("report"), [])

    def test_utc_cutoffs_are_compared_as_instants_and_offsets_required(self):
        self.evidence.update(covered_from="2026-01-14T09:30:00Z", covered_until="2026-01-15T09:30:00Z")
        self.state["daily"]["covered_until"] = self.evidence["covered_until"]
        self.write_metadata()
        self.assertEqual(self.load()["data"]["timezone"], "Asia/Shanghai")

    def test_valueless_html_attributes_and_import_cursor_isolation(self):
        self.s.db.execute("INSERT INTO cursors VALUES(?,?)", (self.p["id"], 7))
        self.write_bundle(HTML.replace("<main>", "<main><p style aria-hidden>普通文本</p>"))
        report = self.load()
        self.assertIn("普通文本", self.files(report)["index.html"])
        self.assertEqual(self.s.db.execute("SELECT seq FROM cursors WHERE project_id=?", (self.p["id"],)).fetchone()[0], 7)

    def test_publish_fake_http_noop_update_and_reconcile_use_standard_identity(self):
        environment = {"FEISHU_APP_ID": "fixture-app", "FEISHU_APP_SECRET": "fixture-secret", "FEISHU_RECEIVE_ID": "fixture-receiver"}
        transport = FakeHTTP()
        with patch.dict(os.environ, environment):
            first_report = self.load()
            first = publish(self.s, self.conf["publisher"], first_report["id"], confirm=True, transport=transport)
            self.assertEqual(first["status"], "completed")
            count = len(transport.calls)
            repeat = publish(self.s, self.conf["publisher"], self.load()["id"], confirm=True, transport=transport)
            self.assertTrue(repeat["no_op"])
            self.assertEqual(len(transport.calls), count)
            self.write_bundle(HTML.replace("合成样本十个", "更正：合成样本八个"))
            second_report = self.load()
            second = publish(self.s, self.conf["publisher"], second_report["id"], confirm=True, transport=transport)
            self.assertEqual(second["status"], "completed")
            for key in ("identity", "document_id", "message_id"):
                self.assertEqual(first[key], second[key])
            creates = [(m, u) for m, u, _ in transport.calls if m == "POST" and u.endswith("/docx/v1/documents")]
            self.assertEqual(len(creates), 1)
            updates = [b for m, u, b in transport.calls if m == "PATCH" and u.endswith("/im/v1/messages/fixture-message")]
            self.assertEqual(len(updates), 1)
            self.assertIn("https://www.feishu.cn/docx/fixture-doc", updates[0]["content"])
            self.assertNotIn("tag\":\"action", self.files(second_report)["card.json"])
            before = len(transport.calls)
            reconcile(self.s, self.conf["publisher"], second["publication_id"], transport=transport)
            self.assertTrue(all(m == "GET" or u.endswith("/tenant_access_token/internal")
                                for m, u, _ in transport.calls[before:]))

    def test_same_period_title_change_refuses_without_document_or_message_writes(self):
        transport = FakeHTTP()
        with patch.dict(os.environ, {"FEISHU_APP_ID": "fixture-app", "FEISHU_APP_SECRET": "fixture-secret",
                                     "FEISHU_RECEIVE_ID": "fixture-receiver"}):
            first_report = self.load()
            snapshot_before = self.files(first_report)
            first = publish(self.s, self.conf["publisher"], first_report["id"], confirm=True, transport=transport)
            self.assertEqual(first["status"], "completed")
            revised = self.load(title="同一周期的修订预览")
            before = len(transport.calls)
            refused = publish(self.s, self.conf["publisher"], revised["id"], confirm=True, transport=transport)
            self.assertEqual(refused["status"], "failed")
            self.assertIn("metadata title change", refused["error"])
            self.assertIsNone(refused["pending"])
            self.assertEqual(transport.calls[before:], [])
            for key in ("identity", "document_id", "message_id", "block_ids", "document_payload_hash", "document_report_id"):
                self.assertEqual(refused[key], first[key])
            self.assertEqual(self.files(first_report), snapshot_before)

    def test_ambiguous_publish_never_retried_by_import(self):
        transport = FakeHTTP()
        transport.fail_card = True
        with patch.dict(os.environ, {"FEISHU_APP_ID": "fixture-app", "FEISHU_APP_SECRET": "fixture-secret", "FEISHU_RECEIVE_ID": "fixture-receiver"}):
            report = self.load()
            unknown = publish(self.s, self.conf["publisher"], report["id"], confirm=True, transport=transport)
            self.assertEqual(unknown["status"], "outcome_unknown")
            before = len(transport.calls)
            self.assertEqual(self.load()["id"], report["id"])
            with self.assertRaises(ValueError):
                publish(self.s, self.conf["publisher"], report["id"], confirm=True, retry=True, transport=transport)
            self.assertEqual(len(transport.calls), before)

    def test_cli_import_and_preview_offline(self):
        config = self.root / "cli-config.json"
        config.write_text(dumps({"state_dir": str(self.s.root)}))
        def cli(*args):
            result = subprocess.run([sys.executable, "-m", "work_assistant", "--config", str(config)] + list(args),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            return json.loads(result.stdout)
        report = cli("import-daily", self.p["id"], "--source-root", str(self.bundle), "--date", PERIOD,
                     "--title", "合成 CLI 提前预览", "--body-scope", "main")
        self.assertEqual(report["data"]["import_options"], {"body_scope": "main"})
        payload = cli("publish", report["id"], "--dry-run")
        self.assertEqual(payload["status"], "dry_run")
        self.assertEqual(payload["document"]["title"], "合成 CLI 提前预览")
        self.assertEqual(self.s.records("publication"), [])
