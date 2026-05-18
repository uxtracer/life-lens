"""Vision pipeline 回归评测脚本。

读 ~/.life_lens/lens.db 里指定 photo_id 的 vision / people 字段,
对照 ground_truth.yaml 的 expected 跑检查,输出 pass/fail 报告。

每次改 prompt 或 vision 模型后跑一遍:
  source .venv_lens/bin/activate
  python tests/eval/run_eval.py                  # 跑全部 10 张
  python tests/eval/run_eval.py --case IMG_0685   # 只跑某张
  python tests/eval/run_eval.py --save           # 把 report 存到 reports/<timestamp>/

依赖:PyYAML(项目应已有);否则 pip install pyyaml
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError:
    print("需要 PyYAML:pip install pyyaml", file=sys.stderr)
    sys.exit(1)

# 让脚本能直接 python tests/eval/run_eval.py 跑(不必装 pkg)
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from life_lens.store import db, repo


TRUTH_PATH = Path(__file__).parent / "ground_truth.yaml"
REPORTS_DIR = Path(__file__).parent / "reports"

# --- ANSI 色,终端可读 ---
RED   = "\033[31m"
GREEN = "\033[32m"
YEL   = "\033[33m"
BLUE  = "\033[34m"
DIM   = "\033[2m"
RST   = "\033[0m"


def _eq_persons(expected: list[str], actual: list[str]) -> bool:
    """persons 集合相等(忽略顺序/重复)。"""
    return set(expected) == set(actual or [])


def _role_check(description: str, roles: dict, window: int) -> list[str]:
    """对每个 name,检查 description 里 **name 之后 window 字符内** 是否出现对应动作关键词。

    只看名字之后的 window 字符,因为中文叙事天然"名字 + 动作"语序("张三举手机自拍")。
    如果同时看名字之前,会把别的人的动作误关联到这个名字(比如"小明举手机自拍" 出现在
    张三位置的窗口前侧 → 误判 hit)。
    失败返回错误列表。"""
    errors = []
    for name, keywords in roles.items():
        if name not in description:
            errors.append(f"role_check: 名字 {name!r} 没出现")
            continue
        positions = []
        start = 0
        while True:
            i = description.find(name, start)
            if i < 0: break
            positions.append(i)
            start = i + len(name)
        hit = False
        for pos in positions:
            tail_start = pos + len(name)
            tail = description[tail_start : tail_start + window]
            # 但有一个例外:tail 里如果**先**出现别的真名(隔开了),后面的关键词不算这个 name 的
            # 例:"张三在右侧,蹲着,小明举手机自拍" — 检查张三时 tail 包含整段,
            # "举手机自拍" 是小明的,不能算张三的。截到下一个名字出现前。
            other_names = [n for n in roles if n != name]
            cut = len(tail)
            for on in other_names:
                idx = tail.find(on)
                if 0 <= idx < cut:
                    cut = idx
            tail = tail[:cut]
            if any(kw in tail for kw in keywords):
                hit = True
                break
        if not hit:
            errors.append(f"role_check: {name!r} 邻域(后 {window} 字,截到下一个名字)没找到 {keywords}")
    return errors


def check_case(case: dict, record: dict) -> dict:
    """对单个 case 跑全部检查。返回 {pass, errors, warns}。"""
    if not record:
        return {"pass": False, "errors": ["db 里没找到 photo_id"], "warns": []}

    vision = record.get("vision") or {}
    people = record.get("people") or {}
    derived = record.get("derived") or {}

    description = (vision.get("description") or "")
    scene = (vision.get("scene") or "")
    tags = vision.get("tags") or []
    names = people.get("names") or []
    face_count = people.get("face_count")

    errors: list[str] = []
    warns: list[str] = []

    # face_count
    exp_fc = case.get("expected_face_count")
    if exp_fc is not None and face_count != exp_fc:
        errors.append(f"face_count {face_count} != expected {exp_fc}")

    # persons 集合
    exp_persons = case.get("expected_persons")
    if exp_persons is not None and not _eq_persons(exp_persons, names):
        errors.append(f"persons {sorted(names)} != expected {sorted(exp_persons)}")

    # description 必须包含
    must_in = case.get("description_must_contain") or []
    missing = [w for w in must_in if w not in description]
    if missing:
        errors.append(f"description 缺关键词: {missing}")

    # description 必须不包含
    must_not = case.get("description_must_not_contain") or []
    leaked = [w for w in must_not if w in description]
    if leaked:
        errors.append(f"description 出现禁词: {leaked}")

    # role_check(多人合影:每个名字附近必须出现对应动作)
    role_cfg = case.get("description_role_check")
    if role_cfg:
        window = int(role_cfg.get("window_chars") or 60)
        roles = role_cfg.get("roles") or {}
        role_errs = _role_check(description, roles, window)
        errors.extend(role_errs)

    # scene 含任一关键词
    scene_any = case.get("scene_keywords_any") or []
    if scene_any and not any(kw in scene for kw in scene_any):
        warns.append(f"scene {scene!r} 没命中任一 {scene_any}")

    # tags 必须包含
    tags_must = case.get("tags_must_contain") or []
    tag_str = " ".join(tags)
    missing_tags = [w for w in tags_must if w not in tag_str]
    if missing_tags:
        warns.append(f"tags 缺: {missing_tags}")

    return {
        "pass": not errors,
        "errors": errors,
        "warns": warns,
        "actual": {
            "description": description,
            "scene": scene,
            "tags": tags,
            "names": names,
            "face_count": face_count,
            "place_name": (derived.get("location_bucket") or {}).get("place_name"),
        },
    }


def main():
    ap = argparse.ArgumentParser(description="Vision pipeline 回归评测")
    ap.add_argument("--root", type=Path, default=Path.home() / ".life_lens",
                    help="数据根目录(默认 ~/.life_lens)")
    ap.add_argument("--case", type=str, default=None,
                    help="只跑某张(用 file 字段 substring 匹配,如 IMG_0685)")
    ap.add_argument("--save", action="store_true",
                    help="把报告存到 tests/eval/reports/<timestamp>/")
    ap.add_argument("--json", action="store_true",
                    help="额外输出机器可读 JSON")
    args = ap.parse_args()

    truth = yaml.safe_load(TRUTH_PATH.read_text(encoding="utf-8"))
    cases = truth.get("cases") or []

    if args.case:
        cases = [c for c in cases if args.case in c.get("file", "") or args.case == c.get("photo_id")]
        if not cases:
            print(f"{RED}没有匹配 {args.case!r} 的 case{RST}", file=sys.stderr)
            sys.exit(2)

    conn = db.connect(db.get_db_path(args.root))
    db.init_schema(conn)
    try:
        results = []
        pass_n = fail_n = warn_n = 0
        for case in cases:
            rec = repo.get_photo(conn, case["photo_id"])
            r = check_case(case, rec)
            r["photo_id"] = case["photo_id"]
            r["file"] = case.get("file")
            r["type"] = case.get("type")
            results.append(r)

            status = f"{GREEN}PASS{RST}" if r["pass"] else f"{RED}FAIL{RST}"
            print(f"\n[{status}] {case.get('file')} — {case.get('type')}")
            print(f"  {DIM}photo_id={case['photo_id']}{RST}")
            actual = r.get("actual", {})
            print(f"  face_count={actual.get('face_count')}  names={actual.get('names')}")
            print(f"  scene={actual.get('scene')!r}")
            print(f"  place={actual.get('place_name')!r}")
            desc = (actual.get("description") or "")
            print(f"  desc: {desc[:120]}{'...' if len(desc) > 120 else ''}")
            for e in r["errors"]:
                print(f"  {RED}✗{RST} {e}")
            for w in r["warns"]:
                print(f"  {YEL}⚠{RST} {w}")

            if r["pass"]: pass_n += 1
            else:         fail_n += 1
            warn_n += len(r["warns"])
    finally:
        conn.close()

    print(f"\n{BLUE}===== 评测汇总 ====={RST}")
    print(f"  PASS: {GREEN}{pass_n}{RST} / {len(cases)}")
    print(f"  FAIL: {RED}{fail_n}{RST}")
    print(f"  WARN: {YEL}{warn_n}{RST}")

    if args.save or args.json:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = REPORTS_DIR / ts
        out_dir.mkdir(parents=True, exist_ok=True)
        report = {
            "timestamp": ts,
            "total": len(cases),
            "pass": pass_n,
            "fail": fail_n,
            "warn": warn_n,
            "results": results,
        }
        (out_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n报告存到: {out_dir}/report.json")

    sys.exit(0 if fail_n == 0 else 1)


if __name__ == "__main__":
    main()
