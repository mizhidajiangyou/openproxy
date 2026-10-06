"""静态站完整性：路由覆盖、零外链、资源存在、CSS 令牌齐全。

为什么用 Python 而不是浏览器跑这些断言：它们全是**文本层的性质**
（有没有外链、某个 id 在不在 HTML 里、CSS 变量有没有被定义），
而 R17 的教训是「mock 只证明接线」—— 真正需要真实浏览器的那部分
（渲染、交互、无横向溢出）由 README 里记的手工/浏览器验证覆盖。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

WEB = Path(__file__).resolve().parents[1] / "web"
JS = WEB / "js"
CSS = WEB / "css"
#: 仓库根（src/ 与 tests/ 的父目录），给跨目录的断言用
ROOT = Path(__file__).resolve().parents[1]

#: 页面路由与前端 ROUTES 声明必须一一对应（前端 route id == 后端导航 id）
PAGE_IDS = ["overview", "usage", "models", "channel", "keys", "settings", "guide"]


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def all_js() -> list[Path]:
    return sorted(JS.rglob("*.js"))


def all_css() -> list[Path]:
    return sorted(CSS.rglob("*.css"))


@pytest.fixture(scope="module")
def app_js() -> str:
    return read(JS / "app.js")


#: 承载文字的颜色令牌。装饰性的（描边、底色、阴影）不在此列。
AA_TEXT_TOKENS = [
    "--ink", "--ink-soft", "--ink-muted", "--ink-faint",
    "--cinnabar", "--indigo", "--mineral", "--gamboge", "--celadon", "--rosewood",
    "--sienna", "--slate", "--axis-text",
]


def _srgb_to_linear(channel: float) -> float:
    """sRGB 分量 → 相对亮度用的线性值（WCAG 2.x 的定义）。"""
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def _relative_luminance(colour: str) -> float:
    r, g, b = (int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def _contrast(a: str, b: str) -> float:
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def _resolve_tokens(block: str) -> dict[str, str]:
    """取出一个主题块里的 ``--token: 值``，并把 ``var(--x)`` 顺着解析到底。"""
    tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6}|var\(--[\w-]+\))", block))
    for _ in range(4):
        for key, value in list(tokens.items()):
            match = re.fullmatch(r"var\((--[\w-]+)\)", value)
            if match and match.group(1) in tokens:
                tokens[key] = tokens[match.group(1)]
    return tokens


def exported_names(source: str) -> set[str]:
    """一个 JS 模块导出的具名符号。覆盖 ``export function/const/class`` 与
    ``export { a, b as c }`` 两种写法。"""
    names = set(
        re.findall(r"export\s+(?:async\s+)?(?:function|const|class|let)\s+(\w+)", source)
    )
    for blob in re.findall(r"export\s*\{([^}]*)\}", source):
        for item in blob.split(","):
            cleaned = item.strip()
            if cleaned:
                names.add(cleaned.split(" as ")[-1].strip())
    return names


def find_named_imports(source: str) -> list[tuple[str, str]]:
    """``import { a, b } from './x.js'`` → ``[("a, b", "./x.js")]``。"""
    return re.findall(r"import\s*\{([^}]*)\}\s*from\s*['\"](\.[^'\"]+)['\"]", source)


#: 本项目自己的模块之间流通的具名 helper。用来做「用到但没导入」的反向检查。
SHARED_HELPERS = {
    "esc", "int", "compact", "percent", "duration", "bytes", "ago", "stamp", "shortDay",
    "el", "delegate", "copy", "toast", "icon", "ICON_NAMES", "debounce", "bindOnce",
    "unbindAll",
    "api", "setAdminToken", "ApiError", "qs",
    "lineChart", "barChart", "donutChart", "sparkline", "legend", "shortNum",
    "scaleY", "scaleX", "ticks", "niceMax", "midY", "maxOf",
}


def strip_comments_and_strings(source: str) -> str:
    """去掉注释与字符串字面量，只留可执行代码。

    不做这一步的话，注释里提到的 ``toast(...)``、模板字符串里的 ``esc()``
    都会被当成「真的调用了」，反向检查会误报到没法用。
    """
    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
        elif ch == "/" and i + 1 < n and source[i + 1] == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                i += 1
            i += 2
        elif ch in "'\"`":
            quote = ch
            i += 1
            while i < n:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    i += 1
                    break
                i += 1
            out.append('""')
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def imported_names(source: str) -> set[str]:
    names: set[str] = set()
    for blob, _spec in find_named_imports(source):
        for item in blob.split(","):
            cleaned = item.strip()
            if cleaned:
                names.add(cleaned.split(" as ")[-1].strip())
    return names


def locally_declared(source: str) -> set[str]:
    """本文件自己声明或导入的顶层符号（含函数/类/const/let/var）。"""
    names = set(exported_names(source))
    names |= set(re.findall(r"(?:async\s+)?function\s+(\w+)", source))
    names |= set(re.findall(r"(?:const|let|var)\s+(\w+)", source))
    names |= imported_names(source)
    return names



class TestLayout:
    def test_required_files_exist(self) -> None:
        for rel in (
            "index.html",
            "css/tokens.css",
            "css/base.css",
            "css/layout.css",
            "css/components.css",
            "js/app.js",
            "js/api.js",
            "js/ui.js",
            "js/charts.js",
            "js/pages/overview.js",
            "js/pages/usage.js",
            "js/pages/models.js",
            "js/pages/channel.js",
            "js/pages/keys.js",
            "js/pages/settings.js",
            "js/pages/guide.js",
            "assets/favicon.svg",
        ):
            assert (WEB / rel).exists(), f"缺少文件 {rel}"

    def test_index_is_the_only_entry(self) -> None:
        """hash 路由意味着后端不需要 SPA fallback，所以只能有一个 HTML 入口。"""
        htmls = sorted(p.relative_to(WEB).as_posix() for p in WEB.rglob("*.html"))
        assert htmls == ["index.html"], f"发现了额外 HTML: {htmls}"

    def test_index_declares_shell_ids(self) -> None:
        html = read(WEB / "index.html")
        for node_id in ("shell", "navList", "view", "pageTitle", "pageSubtitle",
                        "modeSeal", "drawerScrim", "btnTheme", "btnCollapse",
                        "btnMenu", "toasts"):
            assert f'id="{node_id}"' in html, f"index.html 缺少 #{node_id}"

    def test_index_loads_every_stylesheet(self) -> None:
        html = read(WEB / "index.html")
        for sheet in sorted(p.name for p in all_css()):
            assert f"/css/{sheet}" in html, f"index.html 没有引用 {sheet}"

    def test_index_loads_app_as_a_module(self) -> None:
        html = read(WEB / "index.html")
        assert '<script type="module" src="/js/app.js"></script>' in html


class TestNoExternalResources:
    """零外链：页面必须能离线/file:// 跑完，不能依赖任何 CDN。"""

    EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["'](https?:)?//""")

    def test_no_external_urls_in_html(self) -> None:
        assert not self.EXTERNAL.search(read(WEB / "index.html"))

    def test_no_external_urls_in_js(self) -> None:
        for path in all_js():
            found = re.findall(r"""["'`](?:https?:)?//[^"'`\s]+""", read(path))
            # charts.js 里的 SVG 命名空间不是资源引用
            found = [u for u in found if "w3.org/2000/svg" not in u]
            assert not found, f"{path.name} 引用了外部地址: {found}"

    def test_no_external_urls_in_css(self) -> None:
        for path in all_css():
            html = self.EXTERNAL.search(read(path))
            assert not html, f"{path.name} 引用了外部地址: {html.group(0)}"

    def test_no_css_import(self) -> None:
        for path in all_css():
            assert "@import" not in read(path), f"{path.name} 用了 @import"

    def test_no_cdn_font(self) -> None:
        for path in all_css():
            assert "fonts.googleapis" not in read(path)

    def test_no_data_uri_images(self) -> None:
        """纸纹理由 CSS 渐变生成；引 data: 图片会让页面体积与可读性都变差。"""
        for path in all_css():
            assert "data:image" not in read(path), f"{path.name} 用了 data:image"

    def test_no_inline_event_handlers(self) -> None:
        for path in [WEB / "index.html", *all_js()]:
            assert not re.search(r"""\son(?:click|load|error|submit)\s*=""", read(path))


class TestRoutes:
    def test_every_page_id_has_a_nav_entry(self, app_js: str) -> None:
        for page_id in PAGE_IDS:
            assert f"id: '{page_id}'" in app_js, f"app.js 缺少路由 {page_id}"

    def test_no_phantom_routes(self, app_js: str) -> None:
        declared = set(re.findall(r"id: '([a-z]+)', label:", app_js))
        assert declared == set(PAGE_IDS), f"多余或缺失的路由: {declared ^ set(PAGE_IDS)}"

    def test_unknown_hash_falls_back_to_overview(self, app_js: str) -> None:
        assert "DEFAULT_ROUTE = 'overview'" in app_js
        assert "return ROUTES.find((r) => r.id === DEFAULT_ROUTE);" in app_js

    def test_every_page_module_is_imported(self, app_js: str) -> None:
        for page in PAGE_IDS:
            assert f"./pages/{page}.js'" in app_js, f"app.js 未导入 {page}.js"

    def test_no_import_of_a_module_that_does_not_exist(self) -> None:
        for path in all_js():
            for spec in re.findall(r"""from\s+['"](\.[^'"]+)['"]""", read(path)):
                target = (path.parent / spec).resolve()
                assert target.exists(), f"{path.name} 导入了不存在的 {spec}"

    def test_no_symbol_imported_that_the_module_does_not_export(self) -> None:
        """这一条抓到了真实的 bug：``overview.js`` 曾从 ``charts.js`` 导入 ``compact``，
        而 ``compact`` 住在 ``ui.js``。ES module 的具名导入在**链接期**就失败 ——
        浏览器里只报一句 SyntaxError，整个页面白屏，而所有「页面白了」的排查都得
        去看 console。这类错误必须在测试里挡住。"""
        exports = {path.resolve(): exported_names(read(path)) for path in all_js()}
        for path in all_js():
            for names_blob, spec in find_named_imports(read(path)):
                target = (path.parent / spec).resolve()
                wanted = {n.strip().split(" as ")[0] for n in names_blob.split(",") if n.strip()}
                missing = wanted - exports.get(target, set())
                assert not missing, (
                    f"{path.name} 从 {spec} 导入了未导出的符号: {sorted(missing)}"
                )


    def test_no_helper_is_used_without_being_imported(self) -> None:
        """反向检查：**用到**的 helper 必须真的被 import（或在本文件声明）。

        上一条只守「import 的东西对方有没有导出」，反方向是空的 —— 于是
        ``models.js`` 与 ``channel.js`` 用了 ``toast`` 却没 import，点「探测上游」
        直接抛 ``ReferenceError``：后端 200、页面毫无反应、按钮永久禁用，
        而 56 项静态测试全绿。用变异实测过：把 ``overview.js`` 的 import 里的
        ``esc`` 删掉（页面上每个 esc() 都变成 ReferenceError），整套件照样通过。
        """
        for path in all_js():
            source = read(path)
            # 声明集合从**原文**取：strip_comments_and_strings 会把 import 的
            # 模块说明符变成空串，imported_names 就再也匹配不到了。
            declared = locally_declared(source)
            code = strip_comments_and_strings(source)
            used = set(re.findall(r"(?<![\w.$])([a-z]\w*)\s*\(", code))
            used &= SHARED_HELPERS
            missing = used - declared
            assert not missing, f"{path.name} 用到了没导入的 helper: {sorted(missing)}"



class TestDesignTokens:
    @pytest.mark.parametrize(
        "token",
        ["--paper", "--ink", "--cinnabar", "--indigo", "--mineral", "--gamboge",
         "--celadon", "--rosewood", "--series-1", "--series-8", "--font-display",
         "--font-mono", "--sidebar-w", "--rule"],
    )
    def test_token_is_defined(self, token: str) -> None:
        assert f"{token}:" in read(CSS / "tokens.css"), f"缺少设计令牌 {token}"

    def test_dark_theme_overrides_the_semantic_colours(self) -> None:
        dark = read(CSS / "tokens.css").split('[data-theme="dark"]')[1]
        for token in ("--paper", "--ink", "--cinnabar", "--series-1"):
            assert f"{token}:" in dark, f"夜间主题没有覆写 {token}"

    def test_text_colours_meets_wcag_aa(self) -> None:
        """所有承载文字的令牌都要达 AA（4.5:1）。

        这是实测出来的：``--ink-faint`` 原本 2.25:1、``--ink-muted`` 4.01:1、
        ``--gamboge`` 2.78:1，而它们用在表头、次要说明、图例、空态提示上 ——
        是真实内容，不是装饰。

        图表系列令牌被刻意写成 ``var(--语义色)`` 的别名，所以这条断言会**顺着
        别名解析到真正的颜色**；将来谁把语义色调暗，图表会自动跟着走，不会漂移。
        """
        css = read(CSS / "tokens.css")
        light, dark = css.split('[data-theme="dark"]')
        for label, block in (("light", light), ("dark", dark)):
            tokens = _resolve_tokens(block)
            background = tokens["--paper-raised"]
            for name in AA_TEXT_TOKENS + [f"--series-{i}" for i in range(1, 9)]:
                assert name in tokens, f"{label}: 缺少令牌 {name}"
                ratio = _contrast(tokens[name], background)
                assert ratio >= 4.5, (
                    f"{label}: {name} = {tokens[name]} 在 {background} 上只有 {ratio}:1，"
                    f"低于 AA 的 4.5:1"
                )

    def test_every_component_class_used_in_js_exists_in_css(self) -> None:
        """抓「写了类名但没写样式」这类静默失效。"""
        css = "\n".join(read(p) for p in all_css())
        used: set[str] = set()
        for path in all_js():
            html = read(path)
            for match in re.findall(r'class="([^"$]+)"', html):
                used.update(c for c in match.split())
            for match in re.findall(r"""classList\.(?:add|toggle|remove)\('([\w-]+)'""", html):
                used.add(match)
        # 只查本站自定义的类：HTML/JS 关键字类不在这套 CSS 里
        custom = {c for c in used if not c.startswith(("is-", "hidden", "grow", "row", "col-"))}
        # **纯定位钩子**：这些类只给测试用（数柱子、量提示框尺寸），刻意没有样式。
        # 「JS 里用到但 CSS 里没有」这条规则要放它们过 —— 否则要么加一段空样式，
        # 要么改测试，两种都会让真正的静默失效混进来。
        hooks = {
            "bar",  # 柱子的语义标记：让测试能只数柱子而不数悬停命中矩形
            "hv-tip-bg",  # 提示框底板：让测试能量出盒子宽高
        }
        missing = sorted(
            c for c in custom - hooks if f".{c}" not in css and f".{c}:" not in css
        )
        assert not missing, f"JS 里用到但 CSS 里没有的类: {missing}"

        # 反向：钩子必须在 JS 里真的被用到，否则它就成了没人维护的死代码
        for hook in hooks:
            assert hook in used, f"定位钩子 {hook} 已经没有测试在用了，应删掉"


class TestServedOverHttp:
    @pytest.fixture
    def served(self, client: TestClient) -> TestClient:
        return client

    @pytest.mark.parametrize(
        "path",
        ["/", "/css/tokens.css", "/css/base.css", "/css/layout.css",
         "/css/components.css", "/js/app.js", "/js/api.js", "/js/ui.js",
         "/js/charts.js", "/assets/favicon.svg"],
    )
    def test_asset_is_served(self, served: TestClient, path: str) -> None:
        response = served.get(path)
        assert response.status_code == 200, f"{path} → {response.status_code}"
        assert response.content, f"{path} 返回空体"

    @pytest.mark.parametrize("page", PAGE_IDS)
    def test_every_page_module_is_served(self, served: TestClient, page: str) -> None:
        assert served.get(f"/js/pages/{page}.js").status_code == 200

    def test_unknown_path_is_404_not_a_silent_index(self, served: TestClient) -> None:
        """不做 SPA fallback，就不能有 catch-all 把 404 吞成 index.html。

        之前写成 ``assert status in (200, 404)`` —— 只放 3xx/5xx 会失败，
        等于什么都没断言。
        """
        for path in ("/no-such-file.css", "/usage", "/models", "/js/nope.js"):
            assert served.get(path).status_code == 404, f"{path} 不该命中任何页面"

    def test_hash_route_never_reaches_the_server(self, served: TestClient) -> None:
        """hash 部分浏览器根本不会发给服务器；这里只是把「只有 / 是 HTML 入口」
        这件事钉死。"""
        response = served.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert served.get("/usage").status_code == 404


class TestPrivacy:
    def test_no_console_log_of_response_bodies(self) -> None:
        for path in all_js():
            html = read(path)
            assert "console.log" not in html, f"{path.name} 里有 console.log"
            assert "console.debug" not in html, f"{path.name} 里有 console.debug"

    def test_backend_never_exposes_secrets_in_public_dict(self, client: TestClient) -> None:
        import json

        body = client.get("/api/admin/settings").text
        for forbidden in ("upstream_key", "admin_token"):
            assert forbidden not in body, f"/settings 泄露了 {forbidden}"
        assert "opencode.ai" in json.loads(body)["upstream_base"]


class TestNoMojibake:
    """源码里不许出现 U+FFFD（替换字符）。

    **为什么单独守这一条**：它几乎总是「用 shell heredoc 写中文注释」时被截断或
    编码错乱留下的。危害有两层：一是注释读起来像乱码，二是**它会静默地毁掉一个
    关键字**—— 本项目就出现过 `仅` 被写成 `仅\ufffd`、`认出` 被写成 `认\ufffd\ufffd`，
    而后者在 Python 里是**语法错误**（`认出` 被拆成两个 token）。
    """

    def test_no_replacement_character_anywhere(self) -> None:
        bad: list[str] = []
        targets = (
            list((ROOT / "src").rglob("*.py"))
            + list((ROOT / "tests").rglob("*.py"))
            + list((ROOT / "tests").rglob("*.mjs"))
            + list((ROOT / "web").rglob("*.js"))
            + list((ROOT / "web").rglob("*.css"))
            + [ROOT / "README.md", ROOT / "task.md"]
        )
        for path in targets:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if "\ufffd" in line:
                    bad.append(f"{path.relative_to(ROOT)}:{lineno}")
        assert not bad, f"这些行含U+FFFD（乱码）：{bad}"

    def test_python_sources_are_valid_utf8(self) -> None:
        """更严格的一层：按 UTF-8 严格解码，不许靠 errors="replace" 蒙过去。"""
        for path in (ROOT / "src").rglob("*.py"):
            raw = path.read_bytes()
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                pytest.fail(f"{path} 不是合法 UTF-8：{exc}")


class TestClientAddress:
    """客户端该填的 Base URL：``/settings`` 给的 ``base_url_hint`` 已经是**绝对地址**。

    真实缺陷：使用指南页把它又和 ``location.origin`` 拼了一次，两段地址粘在一起显示，
    填进客户端立刻连不上；设置页「复制」按钮复制的也是同一个废地址。这两条把
    「后端返回什么形状」与「前端怎么拼」钉在一起。
    """

    def test_hint_is_already_absolute(self, client: TestClient) -> None:
        hint = client.get("/api/admin/settings").json()["base_url_hint"]
        # 必须是能直接粘进客户端的完整 URL；相对路径一旦出现，前端拼错时无从发现
        assert hint.startswith("http://") or hint.startswith("https://")
        assert hint.endswith("/v1")

    def test_frontend_never_re_prefixes_the_hint(self) -> None:
        for page in ("guide.js", "settings.js"):
            code = strip_comments_and_strings(read(JS / "pages" / page))
            assert "location.origin +" not in code, (
                f"{page} 又把 location.origin 拼到 hint 上（hint 本身已是绝对地址）"
            )

    def test_both_pages_go_through_the_same_helper(self) -> None:
        # 三个出现点（指南页地址框 + 设置页输入框 + 复制按钮）必须同源，
        # 否则修好一处、另一处照旧
        for page in ("guide.js", "settings.js"):
            assert "clientBaseUrl(" in read(JS / "pages" / page), f"{page} 没走 clientBaseUrl"


class TestModelCardContract:
    """模型页读的每个字段，后端都得真的产出。

    E2-41 记的那条系统性缺口：``test_no_symbol_imported_that_the_module_does_not_export``
    只守「import 的东西对方有没有导出」。这一组守的是**跨语言的那条边界** ——
    后端改了字段名而前端没跟上时，页面上出现的是 ``undefined`` 而不是报错。
    """

    def test_catalog_entry_carries_the_capability_fields(self, client: TestClient) -> None:
        items = client.get("/api/admin/models").json()["items"]
        assert items, "模型清单不能是空的"
        for item in items:
            for field in ("id", "name", "context_window", "max_output_tokens", "reasoning"):
                assert field in item, f"模型条目缺字段 {field}: {item.get('id')}"
            assert isinstance(item["context_window"], int)
            assert isinstance(item["max_output_tokens"], int)
            assert isinstance(item["reasoning"], bool)

    def test_every_catalog_model_has_a_nonzero_context(self, client: TestClient) -> None:
        """清单里每个模型都必须真的填了规格。

        静态清单是「这个模型有多强」的唯一事实来源；留 0 等于把「不知道」
        伪装成「模型说」—— 而前端只能显示横杠，看起来像功能坏了。
        """
        items = client.get("/api/admin/models").json()["items"]
        for item in items:
            assert item["context_window"] > 0, f"{item['id']} 没填上下文上限"
            assert item["max_output_tokens"] > 0, f"{item['id']} 没填最大输出"

    def test_reachability_is_always_present_even_when_unknown(
        self, client: TestClient
    ) -> None:
        """没探测过也要有这个键（值为 null）。

        少了它前端会读成 ``undefined``，而 ``undefined`` 与「未探测」在
        界面上长得一模一样 —— 于是「还没测」被显示成「测过了，不可用」。
        """
        items = client.get("/api/admin/models").json()["items"]
        for item in items:
            assert "reachability" in item, f"{item['id']} 缺 reachability 字段"
            assert item["reachability"] is None or isinstance(item["reachability"], dict)

    def test_settings_exposes_the_allowed_effort_values(self, client: TestClient) -> None:
        """前端渲染下拉框，合法档位必须由后端给，不能在前端再写死一份。"""
        from openproxy.domain import VALID_REASONING_EFFORTS

        payload = client.get("/api/admin/settings").json()
        # 直接比领域层那份，而不是写死字面量 —— 写死的话加档位时这条会失败，
        # 而失败原因（"expected 4 got 3"）看不出「真正该改的是别处」。
        assert payload["reasoning_efforts"] == list(VALID_REASONING_EFFORTS)
        assert "reasoning_effort" in payload
        # base_url_hint 在同一份返回里（前端两处都用它）
        assert payload["base_url_hint"].endswith("/v1")

    def test_settings_page_does_not_hardcode_effort_whitelist(self) -> None:
        """前端**不许**再写死一份档位白名单。

        这条守的是一个真实 bug：``effortPreset()`` 里手写了 ``['low','medium','high']``，
        而后端加了 ``max`` 之后它没跟着改 —— 于是「下拉框选 max → 点开关 → 静默变成 low」，
        用户选了拉满却拿到最低档，**且没有任何报错**。
        """
        src = read(JS / "pages" / "settings.js")
        assert "settings.reasoning_efforts" in src, (
            "effortPreset() 应从后端给的 reasoning_efforts 取白名单，而不是硬编码"
        )
        # 具体地：不允许出现这种「三个档位逐一比较」的写法
        for level in ("low", "medium", "high", "max"):
            assert f"value !== '{level}'" not in src, (
                f"settings.js 里仍有硬编码的档位白名单（发现 {level}）；"
                "请改用 settings.reasoning_efforts"
            )

    def test_models_page_reads_only_fields_that_exist(self) -> None:
        """反向检查：页面里读的 ``model.*`` / ``settings.*`` 必须都在上面的契约里。

        多读一个字段就是「后端删了、前端没跟上」—— 页面上一片 ``undefined``。
        """
        known = {
            "model": {
                "id", "name", "note", "available", "latency_ms", "requests",
                "total_tokens", "errors", "avg_latency_ms", "context_window",
                "max_output_tokens", "reasoning", "reachability",
            },
            "settings": {
                # base_url_hint 来自 /settings 路由的返回体（不在 public_dict 里），
                # 其余来自 public_dict()
                "base_url_hint", "reasoning_effort", "reasoning_efforts",
            },
        }
        for page, root in (("models.js", "model"), ("settings.js", "settings")):
            code = strip_comments_and_strings(read(JS / "pages" / page))
            used = set(re.findall(rf"\b{root}\.([a-z_]\w*)", code))
            missing = used - known[root]
            assert not missing, f"{page} 读了后端不存在的字段: {sorted(missing)}"
