"""前端资源的静态一致性（方案 11.2、12）。

**这一层测的不是"前端能跑"，而是"前后端之间的接口没有断"。** 前端脚本靠
元素 id 找挂载点，靠 CSS 变量取颜色。这两类引用都是**字符串**，任何一端改名
都不会在 Python 侧报错：

- id 拼错 → `getElementById` 返回 `null` → 脚本在运行时抛异常，页面停在一半，
  服务端日志里一句话都没有；
- CSS 变量拼错 → `var(--typo)` 静默失效，元素用继承来的颜色渲染，看起来"只是
  样式怪了点"。

两类都只在浏览器里显形，而浏览器不在测试链路上。所以它们在提交前就得撞上。

另外三条 CSP 约束也在这里扫：`style-src 'self'` / `script-src 'self'` 之下，
`innerHTML`、`setAttribute('style', ...)`、`onclick=` 这几类写法会被拦，或者
本来就会破坏"所有 DOM 都由 `util.el` 构建"这条一致性前提（见 `util.js` 的文件
头说明）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import RelayEnv

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
JS_DIR = WEB_DIR / "js"
CSS_FILE = WEB_DIR / "app.css"

JS_FILES = sorted(JS_DIR.glob("*.js"))

BY_ID = re.compile(r"""byId\(\s*['"]([^'"]+)['"]\s*\)""")
CSS_VAR_USE = re.compile(r"var\(\s*(--[a-zA-Z0-9_-]+)")
CSS_VAR_DEF = re.compile(r"^\s*(--[a-zA-Z0-9_-]+)\s*:", re.MULTILINE)


def _app_shell(relay_env: RelayEnv) -> str:
    relay_env.add_room()
    with relay_env.paired_client() as client:
        response = client.get("/app")
    assert response.status_code == 200, response.text
    return response.text


def test_js_files_exist() -> None:
    assert JS_FILES, f"{JS_DIR} 下一个脚本都没有"


@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_script_is_not_empty(path: Path) -> None:
    assert path.read_text(encoding="utf-8").strip()


def test_every_element_id_used_by_js_exists_in_the_shell(relay_env: RelayEnv) -> None:
    """脚本通过 `byId` 找的每个 id，外壳里都得有。

    只做这个方向的检查：外壳里有、脚本没用的 id 是允许的（可能刚加、可能由
    其它脚本用），而脚本要找、外壳里没有的，一定是断的。
    """
    html = _app_shell(relay_env)
    missing: list[tuple[str, str]] = []

    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        for element_id in BY_ID.findall(source):
            if f'id="{element_id}"' not in html:
                missing.append((path.name, element_id))

    assert not missing, "脚本引用了外壳里不存在的 id：" + ", ".join(
        f"{name}→#{element_id}" for name, element_id in missing
    )


def test_every_element_id_used_by_js_is_not_duplicated(relay_env: RelayEnv) -> None:
    """同一个 id 在页面里只能出现一次。

    重复 id 不会报错，`getElementById` 只返回第一个——另一份元素永远收不到
    更新，表现为"界面上有两个一模一样的地方，只有一个会变"。
    """
    html = _app_shell(relay_env)
    used = sorted({element_id for path in JS_FILES for element_id in BY_ID.findall(path.read_text(encoding="utf-8"))})

    for element_id in used:
        count = html.count(f'id="{element_id}"')
        assert count == 1, f"#{element_id} 在页面里出现 {count} 次"


def test_css_custom_properties_are_all_defined() -> None:
    """`var(--x)` 用到的每一个变量都要在同一个文件里有定义。

    `app.css` 是唯一一份样式，所以变量不跨文件、也不会被别处注入——这让
    "全部定义在同一文件里"成为一个可以静态验证的完整条件。
    """
    css = CSS_FILE.read_text(encoding="utf-8")
    defined = set(CSS_VAR_DEF.findall(css))
    used = set(CSS_VAR_USE.findall(css))

    assert used, "app.css 里一个 var() 都没有，这条用例失去意义"
    # 第二参数形式 var(--x, fallback) 允许变量不存在，但本项目的 calc/简写
    # 里没有这种用法；真加了的话这里会报出来，让人显式决定。
    assert not (used - defined), f"未定义的 CSS 变量：{sorted(used - defined)}"


def test_dataset_helper_uses_the_camel_case_converter() -> None:
    """`util.el` 写 `data-*` 必须走 `dataset` 的赋值器。

    `setAttribute('data-' + key, ...)` 看着等价，实际不是：它不做 camelCase →
    kebab-case 转换，键 `boardId` 会写成属性名 `data-boardId`，而 HTML 元素上的
    `setAttribute` 会把属性名小写成 `data-boardid`。于是
    `getAttribute('data-board-id')` 返回 `null`——**区域标签点了没反应，控制台
    里一句话都没有**。这个 bug 单测抓不到、服务端日志也抓不到，是真实浏览器
    跑出来的（M5 验证阶段）。

    这里扫字符串，是因为转换规则本身就在浏览器里，Python 侧无法执行验证；
    能锁住的只有"有没有用那个负责转换的 API"。
    """
    source = (JS_DIR / "util.js").read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    source = re.sub(r"//.*", "", source)

    assert "node.dataset[" in source, "util.el 没有使用 dataset 赋值器"
    assert not re.search(r"""setAttribute\(\s*['"]data-""", source), (
        "util.el 在用 setAttribute 拼 data- 属性名，应改用 node.dataset[key]"
    )


def test_class_name_is_written_as_the_class_attribute() -> None:
    """`util.el` 的 `className` 必须落成 `class` 属性。

    和上面 `dataset` 那条是同一个根因：**`setAttribute` 不做命名转换**。
    `setAttribute('className', 'note')` 写出来的是一个名叫 `classname` 的属性,
    而 CSS 匹配的是 `class`——于是 `app.css` 里 `.note`、`.btn`、`.boards__item`
    全部落空：界面结构、文案、交互都正常，**只是完全没有样式**，而且控制台
    一个错都不报。

    M5 真机验证时发现的就是这个：服务端渲染的外壳有样式（模板里写的是
    `class="..."`），`util.el` 建出来的那部分全是裸的。静态检查当时没发现，
    因为它扫的是"外壳用到的 class 名字在 CSS 里有没有定义"——名字全对。
    """
    source = (JS_DIR / "util.js").read_text(encoding="utf-8")
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    source = re.sub(r"//.*", "", source)

    assert re.search(r"""setAttribute\(\s*['"]class['"]""", source), (
        "util.el 没有把 className 落成 class 属性"
    )
    assert not re.search(r"""setAttribute\(\s*['"]className['"]""", source), (
        "util.el 在用 setAttribute 写 className，落下去会成为 classname 属性"
    )


def test_attribute_names_passed_to_set_attribute_are_lowercase() -> None:
    """交给 `setAttribute` 的属性名一律小写。

    HTML 属性名不区分大小写，浏览器会把它们统一小写成小写形态；写成 camelCase
    时落下去的名字**和你想的不是同一个**（`className` → `classname`、
    `viewBox` → `viewbox`），而且不会报错。SVG 上确实有 camelCase 属性，
    但本项目所有 DOM 都由 `util.el` 建 HTML 元素，碰不到那类。
    """
    pattern = re.compile(r"""setAttribute\(\s*['"]([a-z]+[A-Z][A-Za-z]*)['"]""")
    offenders: list[str] = []
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
        source = re.sub(r"//.*", "", source)
        for name in pattern.findall(source):
            offenders.append(f"{path.name}: {name}")
    assert not offenders, f"setAttribute 收到了 camelCase 属性名：{offenders}"


def test_dataset_keys_are_camel_case() -> None:
    """`dataset: { ... }` 的键必须是 camelCase（不能带连字符）。

    `node.dataset['board-id'] = x` 会生成 `data-board-id` 属性——看着对，实际
    也会生效，但它绕过了命名转换这条约定，之后有人改成 `setAttribute` 拼法时
    会静默出错。统一成 camelCase 只有一种写法，才不会分叉。
    """
    offenders: list[str] = []
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        for block in re.findall(r"dataset:\s*\{([^}]*)\}", source):
            for key in re.findall(r"([A-Za-z_$][\w$]*)\s*:", block):
                if "-" in key:
                    offenders.append(f"{path.name}: {key}")
    assert not offenders, f"dataset 的键必须写成 camelCase：{offenders}"


def test_every_data_attribute_read_is_actually_produced(relay_env: RelayEnv) -> None:
    """JS 里读的每个 `data-*` 属性，都得有人写出来。

    两个来源：服务端渲染的外壳（`data-role` 这类）和 JS 的 `dataset` 字面量
    （camelCase 转 kebab）。两边都不产出的话，读到的就是 `null`——而这正是
    `data-boardId` / `data-board-id` 那次不匹配的形态。
    """
    html = _app_shell(relay_env)
    from_shell = set(re.findall(r"\b(data-[a-z0-9-]+)=", html))

    from_js: set[str] = set()
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        for block in re.findall(r"dataset:\s*\{([^}]*)\}", source):
            for key in re.findall(r"([A-Za-z_$][\w$]*)\s*:", block):
                # camelCase → kebab-case，与 dataset 赋值器的规则一致
                from_js.add("data-" + re.sub(r"(?<!^)(?=[A-Z])", "-", key).lower())

    produced = from_shell | from_js
    read: set[str] = set()
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        read.update(re.findall(r"""getAttribute\(\s*['"](data-[a-z0-9-]+)['"]""", source))
        read.update(re.findall(r"""querySelector(?:All)?\(\s*['"][^'"]*\[(data-[a-z0-9-]+)""", source))

    assert read, "一个 data-* 都没读到，这条用例失去意义"
    assert not (read - produced), f"读了没有人产出的 data-* 属性：{sorted(read - produced)}"


def test_no_dead_custom_properties() -> None:
    """定义了但没人用的变量属于"改了没人受影响"的死代码，清掉。

    这条不是为了洁癖：留着它，下一个来改主题的人会以为改它有效。
    """
    css = CSS_FILE.read_text(encoding="utf-8")
    defined = set(CSS_VAR_DEF.findall(css))
    used = set(CSS_VAR_USE.findall(css))

    assert not (defined - used), f"未被引用的 CSS 变量：{sorted(defined - used)}"


def test_scripts_avoid_csp_blocked_dom_apis() -> None:
    """三类写法不许出现，理由见模块说明。"""
    forbidden = {
        "innerHTML": re.compile(r"\.innerHTML\b"),
        "outerHTML": re.compile(r"\.outerHTML\b"),
        "insertAdjacentHTML": re.compile(r"\.insertAdjacentHTML\b"),
        "document.write": re.compile(r"document\.write\b"),
        "setAttribute('style')": re.compile(r"""setAttribute\(\s*['"]style['"]"""),
        "inline handler": re.compile(r"""setAttribute\(\s*['"]on[a-z]+['"]"""),
    }
    # 注释里会提到这些名字（说明为什么不用），所以要先把注释去掉。
    strip = (
        (re.compile(r"/\*.*?\*/", re.DOTALL), ""),
        (re.compile(r"//.*"), ""),
    )

    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        for pattern, replacement in strip:
            source = pattern.sub(replacement, source)
        for label, pattern in forbidden.items():
            match = pattern.search(source)
            assert not match, f"{path.name} 用了 {label}：{match.group(0)!r}"


def test_scripts_do_not_set_inline_styles() -> None:
    """`el.style.foo = ...` 虽然 CSP 不拦，但它绕开了 `app.css` 这一份样式
    来源——"为什么这个元素的颜色不跟着主题变"就这么来的。"""
    pattern = re.compile(r"\.style\.[a-zA-Z]")
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
        source = re.sub(r"//.*", "", source)
        match = pattern.search(source)
        assert not match, f"{path.name} 直接改了内联样式：{match.group(0)!r}"


def test_stylesheet_covers_every_class_used_by_the_shell(relay_env: RelayEnv) -> None:
    """外壳里出现的 class 必须在 `app.css` 里有定义。

    拼错的症状是"这个块没有样式"——在浏览器里只是看起来朴素，没有任何报错。
    所以连同服务端渲染的外壳一起扫。

    **这条只管"名字对不对得上"，不管"名字有没有生效"。** `className` 被
    `setAttribute` 写成 `classname` 那次，这里全绿：名字一个字都没错，只是
    一个都没落成 `class`。生效与否由
    `test_class_name_is_written_as_the_class_attribute` 负责。
    """
    html = _app_shell(relay_env)
    css = CSS_FILE.read_text(encoding="utf-8")

    classes: set[str] = set()
    for attribute in re.findall(r'class="([^"]*)"', html):
        classes.update(attribute.split())
    # 脚本里通过 className 传的也算（`className: 'a b'` / 字符串拼接的除外）
    for path in JS_FILES:
        source = path.read_text(encoding="utf-8")
        for value in re.findall(r"""className:\s*['"]([^'"]+)['"]""", source):
            classes.update(value.split())

    undefined = sorted(c for c in classes if not re.search(rf"\.{re.escape(c)}\b", css))
    assert not undefined, f"外壳用了但 app.css 里没有定义的 class：{undefined}"
