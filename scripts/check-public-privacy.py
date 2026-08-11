from __future__ import annotations

import csv
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DETAILS = ROOT / "career" / "厦门_AI_Agent_20K以上岗位明细_2026-08-08.csv"
REPORT = ROOT / "career" / "厦门_AI_Agent_20K以上岗位与Harbor_AgentOps_3.2最终报告_2026-08-08.md"
MATURITY = ROOT / "docs" / "项目成熟度审计与v3.6完整遥测交付报告_2026-08-11.md"
PUBLIC_TEXT_ROOTS = [ROOT / "career", ROOT / "docs", ROOT / "README.md"]
DIRECT_RECRUITMENT_URL = re.compile(
    r"https?://(?:m\.)?(?:zhaopin\.com/(?:jobdetail|jobs)|"
    r"zhipin\.com/zhaopin|bebee\.com/cn/jobs)",
    re.IGNORECASE,
)
ANONYMOUS_ID = re.compile(r"C\d{2}\Z")


def public_text_files() -> list[Path]:
    files: list[Path] = []
    for item in PUBLIC_TEXT_ROOTS:
        if item.is_file():
            files.append(item)
            continue
        files.extend(path for path in item.rglob("*") if path.suffix in {".md", ".csv", ".json"})
    return files


def main() -> int:
    errors: list[str] = []
    with DETAILS.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    ids = [row.get("匿名公司ID", "") for row in rows]
    expected_ids = {f"C{index:02d}" for index in range(1, 27)}
    if len(rows) != 28:
        errors.append(f"expected 28 anonymized job rows, found {len(rows)}")
    if any(not ANONYMOUS_ID.fullmatch(value) for value in ids):
        errors.append("every public job row must use an anonymous C01-C26 company id")
    if set(ids) != expected_ids:
        errors.append("public company ids must cover C01-C26 exactly")
    duplicate_counts = {value: ids.count(value) for value in set(ids) if ids.count(value) > 1}
    if duplicate_counts != {"C06": 2, "C13": 2}:
        errors.append(f"unexpected same-company job relationships: {duplicate_counts}")

    for path in public_text_files():
        text = path.read_text(encoding="utf-8-sig")
        if DIRECT_RECRUITMENT_URL.search(text):
            errors.append(f"direct recruitment URL exposed in {path.relative_to(ROOT)}")

    report = REPORT.read_text(encoding="utf-8-sig")
    if "隐私说明" not in report or "公司名称、招聘直达链接" not in report:
        errors.append("career report is missing its public anonymization notice")
    if MATURITY.exists():
        maturity = MATURITY.read_text(encoding="utf-8-sig")
        if "26 家匿名公司" not in maturity or "C01–C26" not in maturity:
            errors.append("maturity report is missing its anonymized-sample disclosure")

    if errors:
        print("Public privacy gate failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("Public privacy gate passed: 28 roles use 26 anonymous company ids and expose no direct recruitment URLs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
