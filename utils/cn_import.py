#!/usr/bin/env python3
"""站点规则格式转换：WMN / Sherlock / Maigret ↔ 本项目 v2（文档 §3.3）。

为什么要有这个脚本（文档 §3.4①）：

    WhatsMyName 691 个站点、跑了 11 年，2026-11-09 归档关闭，作者原话是
    "维护数百个站点检测超出承载力"。它的格式本身没问题，问题是同一批站点
    要在三个生态里各写一遍。这个脚本让别人的规则能一条命令搬进来。

用法：

    # 把 WMN 的站点表转成 v2，输出可以直接贴进 sites_cn.json
    python utils/cn_import.py --from wmn wmn-data.json -o cn.json

    # 只转指定站点，顺便看看能不能过校验
    python utils/cn_import.py --from sherlock data.json --site GitHub --site Zhihu

    # 把本项目的规则导回 Sherlock 格式，回馈上游
    python utils/cn_import.py --from v2 maigret/cn/data/sites_cn.json --to sherlock -o out.json

    # 只看统计，不写文件
    python utils/cn_import.py --from wmn wmn-data.json --dry-run

**注意**：转出来的规则没有经过实测。四字段协议要求「存在」和「不存在」
两种响应能被区分开，这个性质读 JSON 判断不出来（文档 §6.2），
导入后必须用 `utils/cn_selfcheck.py` 真跑一遍。
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from maigret.cn.importers import FORMATS, dump, load  # noqa: E402
from maigret.cn.schema import SchemaError, validate_site  # noqa: E402

# sites_cn.json 里的站点会被强制纳入默认扫描（overlay.load_sites 里写死
# force_include=True）。一次导入几百个站点进去，默认扫描会从 500 涨到 1500，
# 每个用户名的耗时翻三倍——这不是错误，但用户多半没预期到。
FORCE_INCLUDE_WARN_THRESHOLD = 50


def detect_format(path: Path, data) -> str:
    """按内容猜格式，猜不出来才让用户显式指定。

    只认结构特征，不看文件名：用户把文件重命名成 data.json 是常态。
    """
    if isinstance(data, dict) and isinstance(data.get("sites"), list):
        return "wmn"
    if isinstance(data, dict) and isinstance(data.get("sites"), dict):
        # 上游 data.json 有 engines/tags，v2 只有 sites
        if "engines" in data or "tags" in data:
            return "maigret"
        return "v2"
    if isinstance(data, list):
        return "wmn"
    if isinstance(data, dict):
        # Sherlock：每个值都是一个带 url 的对象
        values = [v for v in data.values() if isinstance(v, dict)]
        if values and all("url" in v for v in values):
            return "sherlock"
        return "v2"
    raise ValueError("无法识别输入格式，请用 --from 显式指定")


def filter_sites(sites: dict, only: list, exclude: list) -> dict:
    if only:
        wanted = {name.lower() for name in only}
        sites = {k: v for k, v in sites.items() if k.lower() in wanted}
    if exclude:
        unwanted = {name.lower() for name in exclude}
        sites = {k: v for k, v in sites.items() if k.lower() not in unwanted}
    return sites


def check(sites: dict):
    """按 schema 校验，返回（可用, 待补, 不合格）。

    三档而不是两档：`valid: false` 是**合法**条目（schema 允许它存在，
    因为"这条规则还不能用"本身是要记录的信息），但它不能算"通过校验"——
    一个标着 valid=false 的规则拿去扫描，每次都是"未判定"。
    把两者混成一个数字，用户会以为导入成功了，跑起来却什么都没查到。
    """
    usable, pending, bad = {}, {}, []
    for name, raw in sites.items():
        try:
            validate_site(name, raw)
        except SchemaError as exc:
            bad.append((name, str(exc).split("\n")[0]))
            continue
        if raw.get("valid", True):
            usable[name] = raw
        else:
            pending[name] = raw
    return usable, pending, bad


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="站点规则格式转换（WMN / Sherlock / Maigret / v2）"
    )
    parser.add_argument("input", help="输入文件")
    parser.add_argument(
        "--from",
        dest="src",
        choices=FORMATS,
        default=None,
        help="输入格式（默认按内容自动识别）",
    )
    parser.add_argument(
        "--to", dest="dst", choices=FORMATS, default="v2", help="输出格式（默认 v2）"
    )
    parser.add_argument("-o", "--output", help="输出文件（不写则打印到 stdout）")
    parser.add_argument(
        "--site", action="append", default=[], help="只处理指定站点（可重复）"
    )
    parser.add_argument(
        "--exclude-site", action="append", default=[], help="跳过指定站点（可重复）"
    )
    parser.add_argument(
        "--keep-invalid",
        action="store_true",
        help="保留校验不通过的条目（默认丢弃）。保留时 valid 字段会置为 false",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印统计，不输出转换结果"
    )
    args = parser.parse_args(argv)

    path = Path(args.input)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"读取 {path} 失败：{exc}", file=sys.stderr)
        return 2

    src = args.src or detect_format(path, data)
    try:
        sites = load(src, data)
    except ValueError as exc:
        print(f"解析失败：{exc}", file=sys.stderr)
        return 2

    total = len(sites)
    sites = filter_sites(sites, args.site, args.exclude_site)
    if not sites:
        print("没有匹配的站点（检查 --site / --exclude-site）", file=sys.stderr)
        return 1

    ok, pending, bad = check(sites)

    print(f"输入格式：{src}，共 {total} 个站点", file=sys.stderr)
    if len(sites) != total:
        print(f"按 --site 过滤后：{len(sites)} 个", file=sys.stderr)
    print(
        f"可直接使用：{len(ok)} 个；待补：{len(pending)} 个；不合格：{len(bad)} 个",
        file=sys.stderr,
    )

    if pending:
        # 待补的条目在 _note 里写了为什么不能用，直接把名字列出来便于逐个修
        for name in list(pending)[:10]:
            print(f"  ! {name}：{pending[name].get('_note', '未说明')}", file=sys.stderr)
        if len(pending) > 10:
            print(f"  …… 另有 {len(pending) - 10} 条", file=sys.stderr)

    if bad:
        # 只列前 10 条：几千条失败时全打出来没人看，反而把统计淹了
        for name, reason in bad[:10]:
            print(f"  ✗ {name}：{reason}", file=sys.stderr)
        if len(bad) > 10:
            print(f"  …… 另有 {len(bad) - 10} 条", file=sys.stderr)
        print(
            "  不合格的条目默认丢弃。加 --keep-invalid 可以保留（valid 置 false），"
            "适合先存下来再逐条修。",
            file=sys.stderr,
        )

    output_sites = dict(ok)
    if args.keep_invalid:
        for name, entry in pending.items():
            output_sites[name] = dict(entry, valid=False)
        for name, _reason in bad:
            output_sites[name] = dict(sites[name], valid=False)

    if args.dry_run:
        return 0 if ok else 1

    try:
        result = dump(args.dst, output_sites)
    except ValueError as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 2

    if (
        args.dst == "v2"
        and args.output
        and "sites_cn" in Path(args.output).name
        and len(output_sites) > FORCE_INCLUDE_WARN_THRESHOLD
    ):
        print(
            f"注意：{Path(args.output).name} 里的站点会被强制纳入默认扫描，"
            f"这次写入了 {len(output_sites)} 个，默认扫描会明显变慢。"
            "建议先写到一个单独文件里挑完再合并。",
            file=sys.stderr,
        )

    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=False)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        print(f"已写入 {args.output}", file=sys.stderr)
    else:
        print(text)

    print(
        "提醒：导入的规则没有经过实测。四字段能不能分开「存在」和「不存在」，"
        "读 JSON 判断不出来，请用 utils/cn_selfcheck.py 真跑一遍。",
        file=sys.stderr,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
