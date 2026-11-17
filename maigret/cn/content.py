"""内容页解析：把「一页帖子/资料页」变成带时间的结构化事实。

## 为什么单独一层

判定层（`verdict.py`）回答"这个账号存不存在"，抽取层（`extract.py`）回答
"主页上写着什么字段"。这两层都不回答**"这个人什么时候在什么地方说过什么"**——
而后者才是拼图的主料。

所以这一层只做一件事：**把已经拿到的页面，还原成可核对的（时间, 作者, 原文）三元组**。
它不判断真假、不做推断、不跨页合并——那些是上层（`footprint.py` / 报告 / AI）的事。

## 三条硬约束

1. **不猜。** 页面没写的东西一律返回空，绝不用"大概是这个时间"填一个值。
   时间戳少一位数，推出来的年龄就可能差几年，而这正是本工具最危险的输出。

2. **只解析"平台自己展示的公开内容"。** 这一层不负责取页面，只负责解析；
   取页面的合规判断在 `footprint.py`，两件事分开才能分别测试。

3. **抽不到正文的页面要能被认出来。** 空列表的含义必须是"这页确实没有正文"，
   不能和"解析规则不匹配"混为一谈——后者是 bug，前者是事实。
   `parse_*` 返回 `None` 表示"不是这个结构"，返回 `[]` 表示"结构对但没内容"。
"""

import html as html_module
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Post:
    """一层楼 / 一条发言。

    `posted_at` 保持原始字符串形态（如 `2009-9-18 07:30:30`），不在这里
    归一化成 datetime：一来各站时区不同（Discuz 底部会写 GMT+8，但用户可改），
    二来有些站只给"3 天前"。**归一化放上层，且必须保留原始串**，
    否则一旦算错就再也追不回来。
    """

    author: str = ""
    author_id: str = ""
    posted_at: str = ""
    text: str = ""
    # 楼层锚点（如 pid7216607 / #1234），用于回报里给出可点开的地址
    anchor: str = ""
    # 所在主题的标题。列表页和详情页都可能有，缺了不影响主流程
    thread_title: str = ""
    # 该楼层所属的版块/吧名。"哪个吧"本身就是强线索（如"翠园中学吧"）
    forum: str = ""
    url: str = ""

    def is_empty(self) -> bool:
        return not (self.text or "").strip()


@dataclass
class Profile:
    """资料页上的账号元信息。这些值对推断时间线极有用。"""

    name: str = ""
    uid: str = ""
    registered_at: str = ""
    last_visit: str = ""
    last_post_at: str = ""
    online_hours: str = ""
    post_count: str = ""
    thread_count: str = ""
    signature: str = ""
    # 签名/资料页里出现的站内链接。**这是本模块最有价值的一项**：
    # 论坛关掉了"列出该用户发过的主题"，但用户常常自己在签名里贴帖子地址，
    # 那条地址就是他亲手留下的、可被发现的线索。实测 bbs.newwise.com 的
    # yang11235 签名里就有 `thread-481192-1-1.html`，靠它才能抽到 16 条发言。
    site_links: List[str] = field(default_factory=list)
    # 原始键值对，给"这个站还有别的字段"留出观察空间
    extra: Dict[str, str] = field(default_factory=dict)

    def is_empty(self) -> bool:
        """页面上一个可用字段都没解析出来。

        2026-11-17 独立评审 P2#16：原来只看 name / 注册 / 最后访问 / 上次发表
        四个字段。于是"只有 UID + 回帖数、但昵称被模板差异吃掉"的真实资料页
        会被判成空页 → `parse_profile` 返回 None → 上层报"结构不认识"，
        等于把**一条能用的资料**说成了"没读到"。

        补上 uid / online_hours / post_count / thread_count / signature：
        这些字段任一有值，就说明这确实是一个人的资料页，不是空模板。
        """
        return not any(
            (
                self.name,
                self.uid,
                self.registered_at,
                self.last_visit,
                self.last_post_at,
                self.online_hours,
                self.post_count,
                self.thread_count,
                self.signature,
            )
        )


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_NL_RE = re.compile(r"\n{3,}")


def plain_text(fragment: str) -> str:
    """去标签、解实体、压空白。

    必须解 HTML 实体：Discuz 正文里的 `&quot;` `&nbsp;` `&amp;` 不还原，
    报告里就会出现一堆 `&quot;`，用户看不出那是引号还是别的。
    """
    text = _SCRIPT_STYLE_RE.sub(" ", fragment or "")
    text = _COMMENT_RE.sub(" ", text)
    # <br> 是换行语义，先转成换行再剥标签，否则整段会挤成一行
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    text = text.replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def _norm_name(value: str) -> str:
    """用户名比较用：折叠空白 + 大小写折叠，**只做这两件**。

    2026-11-17 独立评审 P1#10 收紧了这里。原来还会去掉 `-` `_` `.` `·`，
    于是 `a-b` 与 `ab`、`john.smith` 与 `johnsmith` 会被判成同一个账号。
    它是 `same_author()` 的唯一判据，而 `same_author()` 是"这个楼层是不是他发的"
    的唯一防线——`only_author` 过滤、资料页姓名核对都靠它。
    归一化每宽一分，"把别人的话记成他说的"就多一条路。
    宽容的方向恰好是危险的方向，所以这里只保守地做两件无害的事：
        大小写  —— 贴吧存大写、Discuz 存小写，同为一个人的可能性高
        空白    —— 首尾空白、全角空格是常见的展示噪声（折成单个半角空格，
                    不删除，这样 `a b` 仍不等于 `ab`）
    分隔符一律保留：`john_smith` 与 `johnsmith` 在各站都是**两个不同的账号**。
    """
    text = (value or "").strip().replace("\u3000", " ")
    return re.sub(r"\s+", " ", text).lower()


def same_author(candidate: str, target: str) -> bool:
    a, b = _norm_name(candidate), _norm_name(target)
    return bool(a) and a == b


def looks_blocked(html: str, status: Optional[int] = None) -> bool:
    """页面是不是被拦了（验证码/登录墙/风控页）。

    返回 True 时调用方**必须**把结论记成"被拦"，不能记成"没内容"。
    这是本项目反复强调的那条：把"被拦住"说成"没有"是最坏的错。
    """
    if status is not None and status in (401, 403, 407, 412, 429, 468, 503):
        return True
    text = (html or "")
    if not text.strip():
        return True
    # 只看前 3000 字符：风控页特征都在头部，正文里出现"验证码"三个字是正常的
    head = text[:3000]
    markers = (
        "百度安全验证",
        "安全验证",
        "请完成验证",
        "访问异常",
        "检测到异常",
        "人机验证",
        "滑动验证",
        "captcha",
        "Just a moment",
        "Checking your browser",
        "Enable JavaScript and cookies",
    )
    low = head.lower()
    if any(m.lower() in low for m in markers):
        return True
    # Discuz 的权限/登录墙也是 HTTP 200，也要认出来。
    # 注意不能用"请登录"当判据：Discuz 每一页的头部都有登录表单
    # （"请 登录 后使用快捷导航"），拿它判会把所有正常页面都当墙。
    return discuz_permission_wall(text)


# Discuz「用户不存在」的文案。实测（bbs.newwise.com 空间页）：
#     抱歉，您指定的用户空间不存在
# 这一个串必须逐字对上，因为 Discuz 的「提示信息」模板同时被用于
# "没有这个用户"和"没有权限"两种情形——只看到「提示信息」就判"不存在"，
# 会把"我被拦了"写成"这个站没这个人"。
_DZ_ABSENT_MARKERS = (
    "指定的用户空间不存在",
    "指定的用户不存在",
    "您指定的用户不存在",
    "该用户不存在",
    "用户空间不存在",
)

# Discuz 的权限墙文案。这些串在正常页面里不会出现。
_DZ_PERMISSION_MARKERS = (
    "您没有权限",
    "没有权限访问",
    "权限不足",
    "阅读权限高于",
    "抱歉，本帖要求阅读权限",
    "抱歉，您需要登录",
    "需要登录后才能",
    # 版块级访问条件。实测 bbs.newwise.com 的 tid=481192：
    #   「您需要满足以下条件才能访问这个版块  访问条件： N币 ≥ 0  您的信息： N币: 0」
    # 游客连 N币=0 都算"不满足"，所以这类版块对匿名访问**永久不可读**，
    # 不是"暂时被拦"。必须能认出来，否则会被当成"这个帖子没有内容"。
    "才能访问这个版块",
    "访问条件",
    "您需要满足以下条件",
)


def discuz_absent(html_text: str) -> bool:
    """这页是不是在说"没有这个用户"。"""
    text = html_text or ""
    return any(marker in text for marker in _DZ_ABSENT_MARKERS)


def discuz_permission_wall(html_text: str) -> bool:
    """这页是不是权限/登录墙。"""
    text = html_text or ""
    return any(marker in text for marker in _DZ_PERMISSION_MARKERS)


# ---------------------------------------------------------------------------
# Discuz
# ---------------------------------------------------------------------------
# 楼层切分：Discuz 每层是一个 <div id="post_数字"> 区块。
# 用它切而不是靠 table 嵌套，是因为 table 在不同皮肤下层级会变，
# 而 post_ 这个 id 是 Discuz 核心模板里的，十几年没改过。
_DZ_POST_SPLIT = re.compile(r'<div id="post_(\d+)"', re.IGNORECASE)
_DZ_AUTHOR = re.compile(
    r'<div class="authi">\s*<a[^>]*href="[^"]*?'
    r'space(?:-uid-|&(?:amp;)?uid=)(\d+)[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_DZ_TIME = re.compile(r'<em id="authorposton\d+">(.*?)</em>', re.IGNORECASE | re.DOTALL)
_DZ_BODY = re.compile(r'<td class="t_f"[^>]*>(.*?)</td>', re.IGNORECASE | re.DOTALL)
_DZ_TITLE = re.compile(r'<span id="thread_subject">(.*?)</span>', re.IGNORECASE | re.DOTALL)
_DZ_TITLE_ALT = re.compile(r'<title>(.*?)</title>', re.IGNORECASE | re.DOTALL)
_DZ_FORUM = re.compile(
    r'<a href="forum\.php\?mod=forumdisplay[^"]*"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)

# 正文里这些"编辑于"尾巴对推断没用，但会污染时间判断（改帖时间 != 发言时间）
_DZ_EDIT_TAIL = re.compile(
    r"^本帖最后由\s*(?P<who>.{1,30}?)\s*于\s*(?P<when>[\d\-: ]+)\s*编辑\s*", re.DOTALL
)


def parse_discuz_posts(html_text: str) -> Optional[List[Post]]:
    """解析 Discuz 主题页里的楼层。不是 Discuz 结构返回 None。

    `authorid=` 参数可以让服务端只返回某个用户的楼层——这是本模块最有价值的
    能力：**给定"某人在某帖里发过言"这一条线索，就能把他在该帖的全部发言
    和时间戳一次拿全**，不必逐页翻。

    Discuz 的"只看作者"视图不带「共 N 页」的小结，但页脚仍有 page= 链接；
    分页交给调用方（`footprint.py`）处理，本函数只管单页。
    """
    text = html_text or ""
    chunks = _DZ_POST_SPLIT.split(text)
    # split 后结构是 [前导, pid1, 内容1, pid2, 内容2, ...]
    if len(chunks) < 3:
        return None

    title_match = _DZ_TITLE.search(text) or _DZ_TITLE_ALT.search(text)
    title = plain_text(title_match.group(1)) if title_match else ""
    # <title> 常带 "- 版块 - 站点名" 尾巴，取第一段
    if title and " - " in title:
        title = title.split(" - ")[0].strip()

    forum_match = _DZ_FORUM.search(text)
    forum = plain_text(forum_match.group(1)) if forum_match else ""

    posts: List[Post] = []
    for i in range(1, len(chunks) - 1, 2):
        pid = chunks[i].strip()
        body_html = chunks[i + 1]

        author_match = _DZ_AUTHOR.search(body_html)
        uid = author_match.group(1) if author_match else ""
        author = plain_text(author_match.group(2)) if author_match else ""

        time_match = _DZ_TIME.search(body_html)
        posted_at = plain_text(time_match.group(1)) if time_match else ""
        # "发表于 2009-9-18 07:30:30" → "2009-9-18 07:30:30"
        posted_at = re.sub(r"^发表于\s*", "", posted_at).strip()

        body_match = _DZ_BODY.search(body_html)
        body = plain_text(body_match.group(1)) if body_match else ""
        # 去掉"本帖最后由 X 于 T 编辑"前缀：那是改帖时间，不是发言时间
        body = _DZ_EDIT_TAIL.sub("", body).strip()

        posts.append(
            Post(
                author=author,
                author_id=uid,
                posted_at=posted_at,
                text=body,
                anchor=f"pid{pid}",
                thread_title=title,
                forum=forum,
            )
        )
    return posts


_DZ_STAT_LABELS = {
    "注册时间": "registered_at",
    "最后访问": "last_visit",
    "上次活动时间": "last_visit",
    "上次发表时间": "last_post_at",
    "在线时间": "online_hours",
    "回帖数": "post_count",
    "主题数": "thread_count",
    "好友数": "extra",
    "积分": "extra",
    "用户组": "extra",
}


def parse_discuz_profile(html_text: str) -> Optional[Profile]:
    """解析 Discuz 个人资料页。

    字段来自「活跃概况 / 统计信息」两个列表。实测 `bbs.newwise.com`：

        注册时间     2009-1-12 13:23
        最后访问     2012-1-5 16:27
        上次活动时间 2012-1-6 17:41
        上次发表时间 2010-10-7 00:18
        在线时间     1010 小时
        回帖数       1564

    这几个时间点凑在一起就能看出"哪几年在活跃、哪年离开"，
    比单看一条帖子更能定位人生阶段。**只读，不解释。**

    ⚠️ 实测发现**同一页上有三种不同的字段形态**，只认前一种会漏掉一半：

        1. `<li><em>标签</em>值</li>`            —— 活跃概况那几条（注册/最后访问）
        2. `<dt>标签</dt><dd>值</dd>`            —— 部分模板
        3. `<a href="...">回帖数 1564</a>`        —— 统计信息那一行（回帖数/主题数）

    形态 3 原来完全没解析，导致 `post_count` / `thread_count` 一直是空的——
    而"回帖 1564、在线 1010 小时"恰恰是判断"那几年投不投入"的关键数字。
    """
    text = html_text or ""
    if "Powered by Discuz" not in text and "home.php?mod=space" not in text:
        return None

    name_match = re.search(r'<h2[^>]*class="mbn"[^>]*>(.*?)</h2>', text, re.DOTALL)
    if not name_match:
        name_match = re.search(r"<title>(.*?)的个人资料", text, re.DOTALL)
    name = plain_text(name_match.group(1)) if name_match else ""
    # 标题形态是 "yang11235的个人资料"，h2 里也可能是 "yang11235 (UID: 463889)"
    name = re.sub(r"\s*[（(]UID[:：]\s*\d+[)）]\s*$", "", name).strip()
    name = re.sub(r"的个人资料$", "", name).strip()

    # 区块标题不能当用户名。
    #
    # 实测 bbs.wuyou.net 对**不存在的用户名**也返回 200 + 13.7KB 的"个人资料"页
    # （标题是 `neil的个人资料`，软 404），页面上只有「活跃概况」这类区块标题，
    # 没有 UID、没有任何统计值。而 `<h2 class="mbn">` 抓到的正是「活跃概况」，
    # 于是 `Profile(name='活跃概况')` 非空 → 被当成"这个用户存在"，
    # 接着拿走整页里**别人的**发言，把 `184822951@qq.com` 这类联系方式
    # 记到目标用户名头上。
    #
    # 这一条是"把别人的信息记到他头上"，比少报严重得多。
    _dz_section_titles = {
        "活跃概况", "统计信息", "个人资料", "基本资料", "联系方式",
        "教育情况", "工作情况", "个人信息", "相关帖子",
    }
    if name in _dz_section_titles:
        name = ""

    uid_match = re.search(r"\(UID:\s*(\d+)\)", text) or re.search(
        r"UID[:：]\s*(\d+)", text
    )
    uid = uid_match.group(1) if uid_match else ""

    profile = Profile(name=name, uid=uid)

    def assign(label: str, raw_value: str) -> None:
        # `&nbsp;` 已由 plain_text 解成普通空格，但标签文本里可能还带别的不间断
        # 空格（\xa0），不归一就匹配不上映射表
        key = _DZ_STAT_LABELS.get(_clean_label(label))
        if not key:
            return
        cleaned = plain_text(raw_value)
        cleaned = re.sub(r"^[:：\s]+", "", cleaned).strip()
        if not cleaned:
            return
        if key == "extra":
            profile.extra[_clean_label(label)] = cleaned
        elif getattr(profile, key, "") == "":
            setattr(profile, key, cleaned)

    # 形态一：<li><em>标签</em>值</li>
    matched_any = False
    for label, value in re.findall(
        r"<em[^>]*>\s*([^<]{1,12}?)\s*</em>(.*?)</li>", text, re.DOTALL
    ):
        assign(label, value)
        matched_any = True

    # 形态二：<dt>标签</dt><dd>值</dd>
    for label, value in re.findall(
        r"<dt>\s*([^<]{1,12}?)\s*</dt>\s*<dd>(.*?)</dd>", text, re.DOTALL
    ):
        assign(label, value)

    # 形态三：统计信息里的 <a>回帖数 1564</a>
    # 这里不能只认 `<em>`，因为 Discuz 把"好友数 / 记录数 / 回帖数"都做成了
    # 链接按钮。用 `标签+数字` 的模式匹配，比绑死某个标签安全。
    for raw_label, raw_value in re.findall(
        r"<a[^>]*>\s*([^\d<>]{2,8}?)\s*(\d[\d,]*)\s*</a>", text
    ):
        assign(raw_label, raw_value)

    # 形态四：`<li>注册时间: 2004-1-3 21:14</li>` —— 纯文本，没有 <em> 包裹。
    #
    # 实测 bbs.wuyou.net 整站都是这个形态，前三形态一个都匹配不上，
    # 结果真实账号（注册于 2004-01-03）被判成"没有这个人"。
    # 这是**漏报**方向，和软 404 的误报方向相反，但同样要修：
    # 用户看到的会是"这个论坛没有我"，而事实上他 2004 年就注册了。
    #
    # 只认"已知标签 + 冒号 + 值"，不认"任意 `xx: yy`"——
    # 后者会把时区、货币单位、好友列表全当成资料字段。
    for label, value in re.findall(
        r"<li[^>]*>\s*([^\d<>:：]{2,8})\s*[:：]\s*([^<]{1,40})</li>", text
    ):
        assign(label, value)

    # 连一个已知标签都没匹配上，说明不是资料页
    if not matched_any and profile.is_empty():
        return None

    # 有一批 Discuz 站对**不存在的用户名**也返回 200 + 完整模板页（软 404）：
    # 标题写 `<用户名>的个人资料`，但页面里既没有 UID，也没有任何统计值。
    # 实测 bbs.wuyou.net：不存在的 `neil` / `sam` 都返回 13.7KB，
    # 且两者内容几乎相同（都是站务公告）。
    #
    # 判据用两条硬事实，缺一不可：
    #   - 没有 UID（真实资料页一定有 `(UID: 12345)` 或 h2 里的 UID 后缀）
    #   - 且没有任何时间/计数类字段（注册时间、回帖数……）
    #
    # 只有一条时不拒：部分老模板确实不写 UID，但有注册时间，那种是真的。
    if not uid and not any(
        (
            profile.registered_at,
            profile.last_visit,
            profile.last_post_at,
            profile.post_count,
            profile.thread_count,
            profile.online_hours,
        )
    ):
        return None

    # 个人签名单独抽：签名里的外链是强线索（常互链到别的站）
    #
    # ⚠️ 实测 bbs.newwise.com（Discuz X5.0）的签名**不是** `<dd id="sign">`，
    # 而是混在统计列表里的 `<li><em>个人签名</em><table><tr><td>…`。
    # 原来只认 `id="sign"` 一种形态，结果签名一条都没抽到——连带丢掉
    # 签名里那个帖子地址（那是唯一能自动发现的帖子线索）。
    sig_html = ""
    sig_match = re.search(r'<dd[^>]*id="sign".*?</dd>', text, re.DOTALL)
    if sig_match:
        sig_html = sig_match.group(0)
        profile.signature = plain_text(sig_html)
    if not profile.signature:
        # 注意 `<em class="xg1">个人签名&nbsp;&nbsp;</em>`：标签文本后面带
        # `&nbsp;`（解出来是 `\xa0`），所以不能用 `\s*个人签名\s*` 卡死，
        # 要让 `[^<]*` 吃掉这段填充。
        sig_match = re.search(
            r"<em[^>]*>\s*个人签名[^<]*</em>(.*?)</li>", text, re.DOTALL
        )
        if sig_match:
            sig_html = sig_match.group(1)
            profile.signature = plain_text(sig_html)
    profile.signature = (profile.signature or "").strip()

    # 站内链接：签名里出现的帖子地址。
    #
    # ⚠️ 这里传的是**签名的原始 HTML**（`sig_html`），不是剥过标签的
    # `profile.signature`。2026-11-17 独立评审 P1#11：原来传的是后者，
    # 而 `plain_text()` 会把 `<a href="…">我的帖子</a>` 里的地址一起剥掉，
    # 于是"链在文字上"的签名地址全部丢失——只留裸文本 URL 能活。
    # 现有夹具恰好是裸文本形态，所以测试通过、bug 被掩护了。
    # 展示仍用 `profile.signature`（给人看的纯文本），抽链接用原始 HTML。
    profile.site_links = _extract_site_links(text, sig_html)

    return profile if not profile.is_empty() else None


def _clean_label(label: str) -> str:
    """归一标签文本：去掉不间断空格、全角空格与首尾空白。"""
    return (label or "").replace("\xa0", " ").replace("　", " ").strip()


# 帖子的两种地址形态：伪静态 `thread-123-1-1.html` 与查询串 `forum.php?mod=viewthread&tid=123`
_DZ_THREAD_PRETTY = re.compile(r"thread-(\d+)-\d+-\d+\.html")
_DZ_THREAD_QUERY = re.compile(r"(?:forum\.php\?|&(?:amp;)?)mod=viewthread&(?:amp;)?tid=(\d+)")
# 签名里的裸地址常常没写成 <a>，得从纯文本里捞
_DZ_BARE_URL = re.compile(r"https?://[^\s<>\"'）)，。]+")


def _extract_site_links(_html_text: str, signature: str) -> List[str]:
    """从**签名**里收集"本站帖子地址"。

    为什么值得单独做：论坛服务端关掉了"列出该用户发过的主题"
    （`home.php?mod=space&do=thread` 对所有用户都返回「提示信息」），
    所以工具无法自动发现"他发过什么帖"。但**用户自己常在签名里贴帖子地址**，
    那是他主动留下的、可被发现的入口。实测 yang11235 的签名里就有
    `http://bbs.newwise.com/thread-481192-1-1.html`，靠这条链接才抽到了
    16 条带时间戳的发言。

    ## 只看签名，不扫整页（2026-11-16 修正）

    原实现在 `html_text` 里也扫了一遍，理由是"有些人写在个人介绍里而不是签名"。
    实测证明这个理由不成立，而代价很严重：

        bbs.wuyou.net 的资料页侧栏含公共区块（"最新帖子""热门话题"），
        那些 tid 对**所有用户**都一样。结果 `neil` 和 `sam` 都拿到
        `site_links=['403684']`，接着各自去拉同一个帖子的全部楼层，
        于是两个不同的人拿到了 93 条完全相同的内容——
        包含别人的手机号 `13950133910` 和邮箱 `184822951@qq.com`。

    把别人的联系方式记到目标用户名头上，是这个项目最不能犯的错。
    签名是他自己的内容，整页 HTML 不是——所以只认签名。

    代价：写在"个人介绍/自定义字段"里的帖子链接会漏掉。这个代价可以接受，
    因为它只会少查，不会错记。

    只返回 tid 形态的规范地址（`forum.php?mod=viewthread&tid=N`），
    不返回原串：后续 `author_filter_url()` 需要能改造这个地址，
    两种形态它都认，但统一之后下游判断少一层分支。
    """
    tids: List[str] = []
    blob = signature or ""
    for match in _DZ_THREAD_PRETTY.finditer(blob):
        tids.append(match.group(1))
    for match in _DZ_THREAD_QUERY.finditer(blob):
        tids.append(match.group(1))
    for match in _DZ_BARE_URL.finditer(blob):
        url = match.group(0)
        pretty = _DZ_THREAD_PRETTY.search(url)
        if pretty:
            tids.append(pretty.group(1))
            continue
        query = _DZ_THREAD_QUERY.search(url)
        if query:
            tids.append(query.group(1))

    seen, out = set(), []
    for tid in tids:
        if tid and tid not in seen:
            seen.add(tid)
            out.append(tid)
    return out


# ---------------------------------------------------------------------------
# 贴吧（手机版）
# ---------------------------------------------------------------------------
# 手机版页面是服务端渲染的纯 HTML，比 PC 版（前端渲染 + 必须登录）好处理得多。
# 楼层用 <div class="p_content"> 承载正文，作者和楼层时间在相邻的 li 里。
_TB_ITEM = re.compile(
    r'<li[^>]*class="[^"]*floor[^"]*"[^>]*>(.*?)</li>', re.IGNORECASE | re.DOTALL
)
_TB_BLOCK = re.compile(r'<div class="p_content[^"]*"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL)
_TB_AUTHOR = re.compile(
    r'class="[^"]*user_name[^"]*"[^>]*>(.*?)</', re.IGNORECASE | re.DOTALL
)
_TB_DATE = re.compile(
    r'class="[^"]*post_time[^"]*"[^>]*>(.*?)</', re.IGNORECASE | re.DOTALL
)
_TB_TITLE = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_TB_FORUM = re.compile(
    r'<a[^>]*class="[^"]*forum_name[^"]*"[^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)


def parse_tieba_posts(html_text: str) -> Optional[List[Post]]:
    """解析贴吧手机版主题页。

    只在**已登录**时可用（未登录一律 403「百度安全验证」，见
    `docs/content-access-feasibility.md`）。所以这里不做"未登录也能跑"的假设。

    ⚠️ **本解析器尚未用真实登录态页面验证过**——手头没有可用的贴吧登录样本，
    手机版的 class 名（`p_content` / `user_name` / `post_time`）是从公开的
    页面结构反推的。用之前请先 `--dump-response` 看一眼实际结构。

    这里刻意**不做**"整页扫日期当证据"的兜底：那样抽出来的 Post 没有作者，
    在集成层会被当成"这个人说的话"记进去——把别人的话记到用户头上，
    比少记严重得多。结构不认识就返回 None，让上层如实说"没读到"。
    """
    text = html_text or ""
    if not text.strip():
        return None

    title_match = _TB_TITLE.search(text)
    title = plain_text(title_match.group(1)) if title_match else ""
    forum_match = _TB_FORUM.search(text)
    forum = plain_text(forum_match.group(1)) if forum_match else ""

    blocks = _TB_BLOCK.findall(text)
    if not blocks:
        return None

    posts: List[Post] = []
    for block in blocks:
        body = plain_text(block)
        if not body:
            continue
        posts.append(Post(text=body, thread_title=title, forum=forum))

    authors = [plain_text(a) for a in _TB_AUTHOR.findall(text)]
    dates = [plain_text(d) for d in _TB_DATE.findall(text)]
    # 正文块拿得到，但作者/时间在块外：整页扫一遍按顺序配对。
    #
    # ⚠️ 只在**数量完全相等**时才配对（2026-11-17 独立评审 P1#14）。
    # 原来是"第 N 个作者配第 N 条正文，不够就留空"，看起来保守，实际不然：
    # 页面上多一个 `user_name` 元素——比如某层楼引用了别人的发言，
    # 引文里带 `<a class="user_name">`——整条链就前移一位，
    # 正文配上的是**被引用的那个人的名字**。
    # 原作者注释写的是"留空会被上层滤掉，不会错记"，但错位不是留空。
    #
    # 数量不等 = 我们不知道哪个名字对应哪条正文，那就一条都不配。
    # 上层对"作者为空"的处理是丢弃，于是结果是"没读到"而不是"记错了人"——
    # 这正是本项目反复强调的优先级。
    # 作者与时间分开判：两者数量不一定同时相等。
    if authors and len(authors) == len(posts):
        for index, post in enumerate(posts):
            post.author = authors[index]
    if dates and len(dates) == len(posts):
        for index, post in enumerate(posts):
            post.posted_at = _normalize_cn_date(dates[index])
    return posts


_CN_DATE = re.compile(r"(\d{4})\s*[-年/]\s*(\d{1,2})\s*[-月/]\s*(\d{1,2})\s*日?")


def _valid_date(year: str, month: str, day: str) -> bool:
    """年/月/日是不是一个真实存在的日期（含日历校验）。

    只查范围（1-12 月、1-31 日）不够：`2009-02-30`、`2009-04-31` 都过得了
    范围检查，但它们不是日期。放进去的后果是时间线上出现一个不存在的日子，
    而用户会当真——这个字段是给推断用的。
    """
    try:
        y, m, d = int(year), int(month), int(day)
    except (TypeError, ValueError):
        return False
    if not (1990 <= y <= 2100):
        return False
    import datetime

    try:
        datetime.date(y, m, d)
    except ValueError:
        return False
    return True


def _normalize_cn_date(value: str) -> str:
    """把 `2009年8月13日` / `2009-8-13` 统一成 `2009-08-13`，保留时间部分。

    认不出来的**原样返回**（不编一个值）。非法日期（`2020-13-45`）也原样返回：
    编一个合法日期比留着原串更坏，因为报告里看不出哪个是编的。
    """
    text = (value or "").strip()
    match = _CN_DATE.search(text)
    if not match:
        return text
    year, month, day = match.group(1), match.group(2), match.group(3)
    if not _valid_date(year, month, day):
        return text
    rest = text[match.end():].strip()
    rest = re.sub(r"^[T\s]+", " ", rest).strip()
    base = f"{year}-{int(month):02d}-{int(day):02d}"
    return f"{base} {rest}".strip() if rest else base


# ---------------------------------------------------------------------------
# 通用兜底
# ---------------------------------------------------------------------------
def parse_generic_page(html_text: str) -> Optional[Dict[str, str]]:
    """任何页面的最小可用信息：标题、描述、发布时间。

    用途是"用户给了一个本工具不认识的 URL，至少把能读的读出来"，而不是
    假装能解析所有站点。抽不到就是抽不到。
    """
    text = html_text or ""
    if not text.strip():
        return None

    def meta(pattern: str) -> str:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        return plain_text(match.group(1)) if match else ""

    # 三段正则拆开写：合在一行会超出行宽限制，拆开后每段都对应一个语义
    # （description / 发布时间 / 作者），改起来也不用数括号。
    result = {
        "title": meta(r"<title[^>]*>(.*?)</title>"),
        "description": meta(
            r'<meta[^>]+(?:name|property)='
            r'["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']'
        ),
        "published": meta(
            r'<meta[^>]+(?:name|property)='
            r'["\'](?:article:published_time|pubdate|publishdate)["\']'
            r'[^>]+content=["\'](.*?)["\']'
        ),
        "author": meta(
            r'<meta[^>]+(?:name|property)='
            r'["\'](?:author|article:author)["\'][^>]+content=["\'](.*?)["\']'
        ),
    }
    if not any(result.values()):
        return None
    # 发布时间也可能是"2026-11-16T12:00:00+08:00"这种，截到日
    if result["published"]:
        result["published"] = _normalize_cn_date(result["published"])
    return result


# ---------------------------------------------------------------------------
# Discuz Archiver（纯静态精简版）
# ---------------------------------------------------------------------------
# Discuz 自带一个给搜索引擎/无 JS 环境用的 `archiver/` 子站，服务端渲染，
# 结构比论坛正常页面简单得多。**实测它比正常页面强得多**：
#
#   普通作者视图  forum.php?mod=viewthread&tid=436216&authorid=463889  78 KB
#   Archiver 视图 archiver/?tid-436216.html&authorid=463889          5.2 KB
#
# 两者都能拿到同样的 16 条发言，但 Archiver 小 15 倍、无重排、无 JS。
# 楼层结构是：
#
#   <p class="author"><strong>yang11235</strong>发表于 2009-10-29 12:31:30</p>
#   <h3></h3>
#   正文…
#
# 注意正文在 `<p class="author">` **之前**、`<h3>` **之后**——顺序与直觉相反，
# 所以不能用"author 后面跟正文"去切，要按 author 标签的位置分段。
_DZ_ARCHIVER_AUTHOR = re.compile(
    r'<p class="author">\s*<strong>(.*?)</strong>\s*发表于\s*([\d\-: ]+)',
    re.IGNORECASE | re.DOTALL,
)


def parse_discuz_archiver(html_text: str) -> Optional[List[Post]]:
    """解析 Discuz `archiver/` 精简页。

    这一页的正文是裸 HTML（`<br />` 换行、带引用块），没有 `t_f` 容器、
    也没有 pid。所以 `anchor` 留空——由上层按"站点+日期+正文"去重。

    返回 None 表示"这不是 Archiver 页"，空列表表示"是 Archiver 页但没有楼层"。
    """
    text = html_text or ""
    if "Archiver" not in text or 'class="author"' not in text:
        return None

    matches = list(_DZ_ARCHIVER_AUTHOR.finditer(text))
    if not matches:
        return None

    # 标题与版块：`查看完整版本: <a …><strong>标题</strong></a>`
    title = ""
    title_match = re.search(
        r"查看完整版本:\s*<a[^>]*>\s*<strong>(.*?)</strong>", text, re.DOTALL
    )
    if title_match:
        title = plain_text(title_match.group(1))

    posts: List[Post] = []
    for index, match in enumerate(matches):
        author = plain_text(match.group(1))
        posted_at = (match.group(2) or "").strip()
        # 正文：从上一条 author 之后到本条 author 之前。
        # 第一条的正文在第一个 author 之前（页面把正文写在作者署名上方）。
        start = matches[index - 1].end() if index > 0 else 0
        segment = text[start:match.start()]
        # 掐掉上一条署名到第一条之间的页面头（导航、面包屑）
        body = _archiver_body(segment)
        if not body:
            continue
        posts.append(
            Post(
                author=author,
                posted_at=_normalize_cn_date(posted_at),
                text=body,
                thread_title=title,
            )
        )
    return posts


def _archiver_body(segment: str) -> str:
    """从 Archiver 的一段 HTML 里取出正文。

    段里可能混着上一条的署名尾巴、引用块链接、`<h3>` 占位。
    实测正文是"裸文本 + `<br />`"，没有容器标签，所以只能：
        1. 去掉 script/style 与 Archiver 的页面骨架
        2. 去掉引用块的图片链接（`http://…/images/common/back.gif`）
        3. 压平标签后取尾段——正文总在段末（下一条署名之前）
    """
    body = _SCRIPT_STYLE_RE.sub(" ", segment or "")
    # 引用块只留下"某某 发表于 …"的提示，图片链接是噪音
    body = re.sub(r"https?://[^\s<>\"]*images/common/\w+\.gif", " ", body)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"<[^>]+>", " ", body)
    body = html_module.unescape(body)
    # 去页头页脚残留
    for marker in ("查看完整版本", "Powered by", "Archiver"):
        body = body.replace(marker, " ")
    text = re.sub(r"[ \t]+", " ", body)
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line and not line.startswith("<")]

    # 第一条的"段"其实是整个页面头（面包屑 + 标题栏），要去掉才能只剩正文。
    # 实测页头形如：
    #   论坛 › 游戏王 › 资料&提问互助区 › ABSOLUTE POWERFORCE（ABPF）607卡表
    #   ABSOLUTE POWERFORCE… - 资料&提问互助区 - 壬天堂世界 - Powered by Discuz!…
    cleaned = []
    for line in lines:
        if "›" in line:
            continue
        if "Powered by" in line or "Archiver" in line or "Discuz" in line:
            continue
        if "壬天堂" in line:
            continue
        cleaned.append(line)
    return "\n".join(cleaned).strip()


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
_PARSERS = (
    # Archiver 排在最前：它对同一帖子返回的内容更小更干净，
    # 而且与正常页并存时优先用它（实测小 15 倍）。
    ("discuz-archiver", parse_discuz_archiver),
    ("discuz", parse_discuz_posts),
    ("tieba", parse_tieba_posts),
)


def parse_thread(html_text: str, site: str = "") -> Optional[List[Post]]:
    """从一页内容里抽楼层，自动挑解析器。

    `site` 只是提示（`discuz` / `tieba`），不强制——实测发现同一套 Discuz
    模板被大量站点复用，靠 HTML 结构识别比靠域名可靠。

    **返回值必须是三态**，调用方要区分：
        None      所有解析器都不认识这个结构（解析规则没覆盖，是 bug）
        []        结构认识，但页面上确实没有楼层（例如"没有权限"页）
        [...]     抽到了

    把 None 折叠成 []（"if posts:" 那种写法）的后果是：
    "解析规则没覆盖"被显示成"这个人没发过言"，而且没有任何地方会提示异常。
    """
    results: List[Optional[List[Post]]] = []
    for name, parser in _PARSERS:
        try:
            results.append(parser(html_text))
        except Exception as exc:  # noqa: BLE001 — 单站解析失败不该中断整轮
            logger.debug("解析器 %s 失败：%s", name, exc)
            results.append(None)

    for posts in results:
        if posts:
            return posts
    if any(posts is not None for posts in results):
        # 有解析器认了这个结构，只是里面没楼层
        return []
    return None


def parse_profile(html_text: str) -> Optional[Profile]:
    try:
        return parse_discuz_profile(html_text)
    except Exception as exc:  # noqa: BLE001
        logger.debug("资料页解析失败：%s", exc)
        return None


@dataclass
class Entry:
    """一页列表里的一个条目（文章、日志、动态）。
    与 `Post` 的区别：`Post` 是"某人在某个帖子里说的一句话"（有作者、有楼层），
    `Entry` 是"这个人的主页上列出的一条内容"（有标题、有时间、指向详情页）。
    两者都最终变成痕迹，但来源不同，排查时要能分清。
    """

    title: str = ""
    url: str = ""
    date: str = ""
    summary: str = ""
    # 详情页还是列表页
    kind: str = "item"

    def is_empty(self) -> bool:
        return not (self.title or "").strip()


# ---------------------------------------------------------------------------
# 内容条目抽取
# ---------------------------------------------------------------------------
# 博客园：每个 <div class="day"> 块里有 "posted @ 2026-11-01 14:57"，
# 同块内的 <a class="postTitle2"> 是文章标题。实测 www.cnblogs.com/dudu。
_CB_DAY = re.compile(r'<div class="day"[^>]*>(.*?)(?=<div class="day"|$)', re.DOTALL | re.IGNORECASE)
_CB_POSTED = re.compile(r"posted\s*@\s*(\d{4}-\d{1,2}-\d{1,2}(?:\s+\d{1,2}:\d{2})?)")
_CB_TITLE = re.compile(
    r'<a[^>]*class="postTitle2[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.DOTALL | re.IGNORECASE,
)
_CB_ARCHIVE = re.compile(r"/p/archive/")

# 通用兜底：把页面上的日期与附近的链接配对。
# 精度不如专用解析器，但能覆盖大量"本站没写解析器"的博客/文档站。
_GENERIC_DATE = re.compile(
    r"(?<![A-Za-z0-9])(20\d{2}[-/年.]\d{1,2}[-/月.]\d{1,2})(?!\d)"
)
_GENERIC_LINK = re.compile(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)


def parse_entries(html_text: str, base_url: str = "") -> Optional[List[Entry]]:
    """从一页列表里抽条目。

    返回值的三态与 `parse_thread` 一致，必须区分：
        None     不是网页（JSON/空响应/二进制）—— 这类页面本来就不该走 HTML 解析
        []       是网页，但上面没有"带日期的条目"
        [...]    抽到了

    为什么要分开 `None` 和 `[]`：很多平台的主页（GitHub、MyAnimeList、
    Twitch）**本来就不列内容条目**，只显示资料与统计。把这种情形报成
    "结构不认识"，用户会以为工具出问题了；报成"这页没有列内容"才是实话。

    三级策略，顺序不能反：
        1. 专用解析器（博客园这类结构明确的）
        2. **内嵌 JSON 列表**（CSDN、掘金这类前端渲染站）
        3. 通用配对（日期 + 附近链接）

    为什么第 2 级要排在通用配对之前：中文平台大量用 Vue/Nuxt 做前端，
    文章列表不在 HTML 里，而在 `window.__INITIAL_STATE__` 这种内嵌 JSON 里。
    对这类页面，通用配对**永远抽到 0 条**（页面上根本没有"日期+链接"的
    HTML 形态），于是"这站有 20 篇文章"被显示成"这页没有列内容"。
    实测 CSDN 博客主页：通用配对 0 条，内嵌 JSON 里 20 条全带时间戳。

    为什么需要通用兜底：主扫描会在几百个平台上命中账号，
    给每个平台写解析器不现实；而"日期旁边的那条链接"这个形态，
    在绝大多数博客/文档站上都成立。
    """
    text = html_text or ""
    if not text.strip():
        return None

    entries = _parse_cnblogs(text)
    if entries:
        return entries

    # 判断"这是不是网页"。不是网页的直接返回 None，不假装能解析。
    head = text[:2000].lower()
    looks_html = any(
        marker in head
        for marker in ("<html", "<!doctype", "<body", "<div", "<head")
    )
    if not looks_html:
        return None

    entries = _parse_embedded_json(text, base_url)
    if entries:
        return entries

    return _parse_generic_entries(text, base_url)


def _parse_cnblogs(html_text: str) -> List[Entry]:
    blocks = _CB_DAY.findall(html_text)
    if not blocks:
        return []
    out: List[Entry] = []
    seen = set()
    for block in blocks:
        posted = _CB_POSTED.search(block)
        date = posted.group(1) if posted else ""
        for url, raw_title in _CB_TITLE.findall(block):
            if _CB_ARCHIVE.search(url or ""):
                continue  # "2026年11月" 这种归档链接不是文章
            title = plain_text(raw_title)
            if not title or url in seen:
                continue
            seen.add(url)
            out.append(Entry(title=title, url=url, date=date, kind="article"))
    return out


# ---------------------------------------------------------------------------
# 内嵌 JSON 列表抽取（前端渲染站）
# ---------------------------------------------------------------------------
# 中文平台大量用 Vue/Nuxt/Next 做前端，文章列表不在 HTML 里，而在页面内嵌的
# JSON 里。实测三种常见的挂载点：
#
#   CSDN      window.__INITIAL_STATE__ = {...}
#   Nuxt 站   window.__NUXT__ = {...}
#   Next 站   <script id="__NEXT_DATA__" type="application/json">{...}</script>
#
# 只认这三种。不写"扫所有 <script> 里的 JSON"那种通用做法——那会把埋点、
# A/B 配置、广告位全当成文章列表，产出一堆没有意义的条目。
_EMBEDDED_PATTERNS = (
    # (取 JSON 文本的正则, 说明)
    (re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;\s*</script>", re.S), "INITIAL_STATE"),
    (re.compile(r"window\.__NUXT__\s*=\s*(\{.*?\})\s*;\s*</script>", re.S), "NUXT"),
    (
        re.compile(
            r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
        ),
        "NEXT_DATA",
    ),
)

# 一个 dict 里哪些键名像"内容列表"。
# 实测 CSDN 用 `latelyList`（最近文章），掘金/其他站常见 `articleList`
# `postList` `list` `items`。只认这些白名单键，不做"遍历所有 list"——
# 后者会把 `tags`、`images`、`adList` 也当成文章。
_LIST_KEYS = (
    "latelyList", "articleList", "articles", "postList", "posts",
    "noteList", "contentList", "list",
)

# 父键命中这些词 → 这个列表是**别人的**内容，不能要。
#
# 2026-11-17 独立评审 P1#12：`_walk_lists` 原来只做"取最长的那个列表"，
# 而 `_LIST_KEYS` 里有 `list`、`posts` 这类通用键。推荐位/热榜用的正是
# 这种结构，而且常常比"本人文章"更长：
#     {"user": {"articleList": [2 条本人文章]},
#      "recommend": {"list": [6 条**别人**的文章]}}
# → 抽出 6 条别人的文章，挂在"他的主页内容"下面。
#
# 只按"最长"选，是在赌"本人的列表一定最长"——这个前提不成立。
# 所以再加一道：父键像推荐位就直接跳过。
_LIST_PARENT_DENY = re.compile(
    r"recommend|related|similar|hot|popular|suggest|random|guess|interest|"
    r"rank(?:ing)?|top\b|trending|discover|explore|other",
    re.IGNORECASE,
)

# 时间字段候选，按优先级。都是**秒或毫秒时间戳**或 ISO 串，
# 统一交给 `_entry_date` 归一。
_TIME_KEYS = ("postTime", "created_at", "createdAt", "createTime", "create_time",
              "publishTime", "published_at", "publish_time", "date", "ctime")

# 标题字段候选
_TITLE_KEYS = ("title", "article_title", "articleTitle", "name", "edit_title")

# 详情页地址候选
_URL_KEYS = ("url", "article_url", "link_url", "href", "share_url")


def _entry_date(value: Any) -> str:
    """把内嵌 JSON 里的时间值归一成 `YYYY-MM-DD`。

    三种形态都认（都在实测样本里出现过）：
        1640228145          秒时间戳
        1640228145000       毫秒时间戳
        "2023-04-11T21:17:01+08:00"   ISO 串

    认不出来返回空串。**不用当前时间兜底**——那会凭空造出一条"最近活跃"。
    """
    if isinstance(value, (int, float)):
        return _epoch_date(value)
    text = str(value or "").strip()
    if not text:
        return ""
    # 纯数字串也当时间戳（JSON 里偶尔是字符串形态）
    if text.isdigit():
        return _epoch_date(float(text))
    return _normalize_cn_date(text)


def _epoch_date(number: float) -> str:
    """时间戳 → 日期。量级判断秒/毫秒，越界返回空串。"""
    if number <= 0:
        return ""
    seconds = number / 1000.0 if number > 1e11 else number
    try:
        import datetime

        moment = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    if not 1990 <= moment.year <= 2100:
        return ""
    return moment.strftime("%Y-%m-%d")


def _walk_lists(node: Any, depth: int = 0, limit: int = 4000) -> List[dict]:
    """在 JSON 里找"像内容列表"的数组，返回其中最长的那个列表的成员。

    为什么取"最长的那个"而不是全部合并：一个页面里可能有多个列表
    （推荐位、热门、本人文章）。实测 CSDN 的 `latelyList`（20 条）与
    `columnModule`（11 条）同时存在，合并会把专栏也当成文章发出去。
    取最长的一条通常就是"这个人自己的内容"，而且宁可少给也不错给。

    **但"最长"不能单独作判据**（2026-11-17 独立评审 P1#12）：推荐位
    常常比本人内容更长，而它的键名（`recommend`、`related`…）落在
    `_LIST_KEYS` 的通用项 `list` / `posts` 上，一样会被命中。
    所以先按父键排除推荐类容器，再在剩下的里取最长。

    深度上限 12：正常页面数据不会更深，防止异常响应把递归打穿。
    """
    best: List[dict] = []
    # 栈里带上"这个节点是从哪个键走下来的"：判别推荐位要看**父键**
    # （`recommend` 下面的 `list` 才是推荐位，单独看 `list` 看不出来）。
    stack: List[Tuple[Any, int, str]] = [(node, 0, "")]
    seen = 0
    while stack:
        current, level, parent_key = stack.pop()
        seen += 1
        if seen > limit or level > 12:
            continue
        if isinstance(current, dict):
            for key, value in current.items():
                # 父键或本键任一像推荐位就跳过
                if _LIST_PARENT_DENY.search(str(key)) or (
                    parent_key and _LIST_PARENT_DENY.search(parent_key)
                ):
                    # 仍然要往里走一层：被排除的容器之下不会再有本人的列表，
                    # 但跳过整棵子树会漏掉"recommend 里嵌了自己的东西"这种
                    # 罕见结构——代价只是多几次栈操作，不值得为它冒漏报风险。
                    stack.append((value, level + 1, str(key)))
                    continue
                if (
                    key in _LIST_KEYS
                    and isinstance(value, list)
                    and value
                    and isinstance(value[0], dict)
                ):
                    if len(value) > len(best):
                        best = [item for item in value if isinstance(item, dict)]
                stack.append((value, level + 1, str(key)))
        elif isinstance(current, list):
            for item in current[:40]:
                stack.append((item, level + 1, parent_key))
    return best


def _parse_embedded_json(html_text: str, base_url: str) -> List[Entry]:
    """从页面内嵌的 JSON 里抽内容条目。抽不到返回空列表（不是 None）。

    返回空列表而不是 None 的含义要分清：`None` 由 `parse_entries` 在
    "根本不是网页"时返回；这里返回 `[]` 表示"是网页，但内嵌 JSON 里
    没有可识别的内容列表"——调用方据此继续走通用配对。
    """
    import json
    from urllib.parse import urljoin

    for pattern, _label in _EMBEDDED_PATTERNS:
        match = pattern.search(html_text)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except (TypeError, ValueError):
            continue
        if not isinstance(data, (dict, list)):
            continue

        items = _walk_lists(data)
        if not items:
            continue

        out: List[Entry] = []
        seen = set()
        for item in items:
            title = ""
            for key in _TITLE_KEYS:
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    title = candidate.strip()
                    break
            if not title:
                continue

            url = ""
            for key in _URL_KEYS:
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    url = candidate.strip()
                    break
            url = urljoin(base_url, url) if (url and base_url) else url

            date = ""
            for key in _TIME_KEYS:
                candidate = item.get(key)
                if candidate in (None, ""):
                    continue
                date = _entry_date(candidate)
                if date:
                    break

            key = (title, url)
            if key in seen:
                continue
            seen.add(key)
            out.append(Entry(title=title, url=url, date=date, kind="item"))
        if out:
            return out
    return []


def _parse_generic_entries(html_text: str, base_url: str) -> List[Entry]:
    """日期 + 附近链接配对。

    配对窗口取日期**前后各 200 字符**：太窄会漏掉"日期在标题下方"的排版，
    太宽会把相邻两条内容串起来。200 是实测下来覆盖两种常见排版的值。

    刻意不做"整页所有链接都当条目"——那会把导航栏、页脚、广告
    全部记成"这个人发过的内容"。

    标题还要过一道 `_looks_like_title`：实测豆瓣活动页会把
    「发起人：LEE-H」「1309人参加」这种**别人的名字与计数**抽成标题，
    而报告里就会出现"他在 2011-12-30 发布了 LEE-H"。
    把别人的名字记到他头上，比少记一条严重得多。
    """
    from urllib.parse import urljoin

    # 先把所有日期的位置收出来：窗口不能越过相邻的日期。
    #
    # 2026-11-17 独立评审 P1#13：原来固定取 `日期前 200 / 后 200` 字符，
    # 于是侧栏里任意一个链接只要落在某个日期的 ±200 内，就会被**赋上那个日期**。
    # 实测症状：本人条目 2020-05-05 周围侧栏的三条链接全部被写成
    # "2020-05-05 发布"，其中两条是别人的文章——日期张冠李戴 + 内容张冠李戴。
    # 把窗口切在相邻日期处，一条链接就不可能继承隔壁条目的日期。
    date_spans = [(m.start(), m.end(), m.group(1)) for m in _GENERIC_DATE.finditer(html_text)]

    out: List[Entry] = []
    seen = set()
    for index, (d_start, d_end, d_value) in enumerate(date_spans):
        prev_end = date_spans[index - 1][1] if index > 0 else 0
        next_start = (
            date_spans[index + 1][0] if index + 1 < len(date_spans) else len(html_text)
        )
        start = max(prev_end, d_start - 200)
        end = min(next_start, d_end + 200)
        window = html_text[start:end]
        for link in _GENERIC_LINK.finditer(window):
            url, raw_title = link.group(1), link.group(2)
            title = plain_text(raw_title)
            # 太短的链接文本多半是"阅读全文""更多"这类按钮
            if len(title) < 4 or not _looks_like_title(title):
                continue
            # 链接紧跟着「发起人：」这类标签时，它是**别人的名字**，不是内容标题。
            # 只看 link 文本判不出来（"LEE-H" 本身干干净净），必须看前文。
            if _META_LABEL_HEAD.search(window[max(0, link.start() - 24): link.start()]):
                continue
            # 落在侧栏/推荐位容器里的链接是别人的内容，不要。
            if _in_denied_container(window, link.start()):
                continue
            key = (title, url)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                Entry(
                    title=title,
                    url=urljoin(base_url, url) if base_url else url,
                    date=d_value,
                    kind="item",
                )
            )
    return out


# 侧栏 / 推荐位 / 热榜的容器开标签。落在这些容器里的链接是**别人的**内容。
_CONTAINER_OPEN = re.compile(r"<(div|section|ul|ol|aside|nav)\b[^>]*>", re.IGNORECASE)
_CONTAINER_DENY = re.compile(
    r"sidebar|aside|recommend|related|hot|popular|trending|rank|guess|"
    r"tuijian|hotlist|toplist|other|friend|link",
    re.IGNORECASE,
)


def _in_denied_container(window: str, position: int, lookback: int = 1200) -> bool:
    """`position` 处是否落在侧栏/推荐位容器内。

    做法：往回找**最近的一个容器开标签**（`<div class="hot">` 这种），
    看它的 class/id 是否命中 `_CONTAINER_DENY`。

    这是启发式，不是 HTML 解析：真正的容器配对需要 DOM 树，而这里刻意
    不引第三方解析器（规则层要保持零依赖）。启发式的误判方向是可控的：
    它只会**少收**，不会多收——而多收才是"把别人的文章记成他的"。
    找不到容器、或开标签的 class 落在窗口外时，按"不在侧栏里"处理（保守）。
    """
    begin = max(0, position - lookback)
    segment = window[begin:position]
    last = None
    for match in _CONTAINER_OPEN.finditer(segment):
        last = match
    if last is None:
        return False
    return bool(_CONTAINER_DENY.search(last.group(0)))


# 形如"标签：值"的串不是标题，是元信息行。
# 实测误抽样本：「发起人：LEE-H」「时间：2011年12月30日」「标签：豆瓣说 下线」。
#
# 两个用途，形态不同，所以两个正则：
#   _META_LABEL_HEAD  用在"链接前文"上——它必须**以冒号结尾**（后面紧跟链接）
#   _META_LABEL_LINE  用在"链接文本自身"上——从头匹配到值
_META_WORDS = (
    "发起人|组织者|时间|地点|标签|作者|参与者|人数|浏览|评论|阅读|"
    "更新于|发表于|发布于|来自"
)
_META_LABEL_HEAD = re.compile(r"(?:" + _META_WORDS + r")\s*[:：]\s*(?:<[^>]+>\s*)*$")
_META_LABEL_LINE = re.compile(r"^(?:" + _META_WORDS + r")\s*[:：]")
# 纯数字/计数串（"1309人参加"、"23"）
_COUNT_ONLY = re.compile(r"^[\d,.\s]*(?:人参加|次浏览|条评论|个|篇|人)?$")


def _looks_like_title(value: str) -> bool:
    """这个链接文本像不像一个内容标题。

    判据刻意保守——只排除"明确是元信息"的形态，不做"必须是名词短语"
    这类语义判断（那需要模型，且会误伤）。三条排除：

        1. 带"标签：值"形态的元信息行（发起人：X / 时间：Y）
        2. 纯数字或纯计数（1309人参加）
        3. 只有日期没有其他字（2011年12月30日）

    第 3 条来自实测：日期窗口里最常见的"链接文本"就是日期本身。
    """
    text = (value or "").strip()
    if not text:
        return False
    if _META_LABEL_LINE.match(text):
        return False
    if _COUNT_ONLY.match(text):
        return False
    # 只剩日期（去掉日期后不足 2 个字）→ 不是标题
    stripped = _GENERIC_DATE.sub("", text).strip(" \t\r\n-—:：/|,，。、")
    return len(stripped) >= 2


# ---------------------------------------------------------------------------
# 线索抽取：从正文里挑"可被后续利用的东西"
# ---------------------------------------------------------------------------
# 这里**只抽形态明确、误报低的**。地名/学校/单位这类要靠 AI 判断，
# 用正则硬抽必然把"壬天堂世界"当成"天堂"，把"游戏王"当成单位。
_PLACE_SUFFIX = (
    "省|市|区|县|镇|街道|路|中学|大学|学院|高中|初中|小学|"
    "职业学院|附属中学|实验学校"
)
_PLACE_HINT = re.compile(
    r"(?:^|[\s，,。.、；;：:！!？?【\[(（\"'“‘])"
    r"([\u4e00-\u9fff]{2,10}?(?:" + _PLACE_SUFFIX + r"))"
)
# 中文没有词边界，"我在深圳中学读书" 会被整段捕获成"我在深圳中学"。
# 所以捕获后要剥掉开头的动词/代词/介词——这些字不可能是地名的一部分。
# 不做这一步，抽出来的"地名"全是"我在XX"，AI 分析时会被当成噪音。
_PLACE_LEAD_STOP = set("我你他她它们的是在了有去来到说想要个和与跟对把被这那")


def _strip_place_lead(value: str) -> str:
    """剥掉地名开头的非地名字符（动词/代词/介词）。"""
    text = value
    while text and text[0] in _PLACE_LEAD_STOP:
        text = text[1:]
    return text


_CONTACT_HINT = re.compile(
    r"(?:[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})"
    r"|(?:1[3-9]\d{9})"
)
_QQ_HINT = re.compile(r"(?:QQ|扣扣|企鹅)\s*[:：]?\s*(\d{5,12})", re.IGNORECASE)


def mine_clues(text: str) -> Dict[str, List[str]]:
    """从一条正文里挑出"能拿去继续查"的线索。

    只返回**出现在原文里的字面值**，不做归一化、不做同义扩展。
    上层拿这些值去构造下一轮查询（人工搜索或站内翻页），
    判断"这条线索说明什么"是 AI 的事。
    """
    body = text or ""
    places = []
    for match in _PLACE_HINT.findall(body):
        cleaned = _strip_place_lead(match)
        # 剥完之后太短的多半是被切碎的词，丢掉比留下更安全
        if len(cleaned) >= 2:
            places.append(cleaned)
    contacts = _CONTACT_HINT.findall(body)
    qq = _QQ_HINT.findall(body)

    def uniq(values: List[str]) -> List[str]:
        seen, out = set(), []
        for value in values:
            key = value.strip()
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out

    return {
        "places": uniq(places),
        "contacts": uniq(contacts),
        "qq": uniq(qq),
    }
