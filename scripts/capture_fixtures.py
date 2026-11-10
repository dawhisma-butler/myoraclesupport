"""Capture live ARU XML fixtures for tests and print discovery info (ids, abstracts).

usage: python scripts/capture_fixtures.py --token-file ~/.mos/token.txt --out tests/fixtures/
Prints patch metadata only; never prints the token.
"""
import argparse
import os
from pathlib import Path

from opatchbot.aru import AruClient, parse_search_xml

PT_FAMILY, HCM, CS, DB = 21917, 21858, 21591, 9480
PS92, DB19, DB21 = 27001300090200, 600000000101150, 600000000153639
WIN, LNX, GENERIC = 233, 226, 2000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--token-file", default="~/.mos/token.txt")
    ap.add_argument("--out", default="tests/fixtures")
    a = ap.parse_args()
    tok = Path(os.path.expanduser(a.token_file)).read_text().strip()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    c = AruClient(lambda: tok)

    print("== FSCM family candidates ==")
    for pid, name in c.metadata("aru_products"):
        n = name.lower()
        if "peoplesoft" in n and any(k in n for k in ("fscm", "financ", "supply")) and "fusion" not in n and "migration" not in n:
            print(f"  {pid}  {name}")

    def save(name: str, **params):
        text = c._get_text("https://updates.oracle.com/Orion/Services/search", params)
        (out / name).write_text(text)
        patches = parse_search_xml(text)
        rows = sorted(((p.released.isoformat(), p.number, p.abstract[:72], p.release_name, p.platform_id) for p in patches), reverse=True)
        print(f"== {name} params={params} -> {len(patches)} patches ==")
        for r in rows[:10]:
            print("  ", " | ".join(str(x) for x in r))
        return patches

    save("search_hcm_92_233.xml", product=HCM, release=PS92, platform=WIN)
    save("search_cs_92_233.xml", product=CS, release=PS92, platform=WIN)
    save("search_hcm_92_generic.xml", product=HCM, release=PS92, platform=GENERIC)
    save("search_hcm_92_norel_233.xml", product=HCM, platform=WIN)
    save("search_db19_226.xml", product=DB, release=DB19, platform=LNX)
    save("search_db21_226.xml", product=DB, release=DB21, platform=LNX)
    save("patch_6880880.xml", bug=6880880)
    save("patch_40016683.xml", bug=40016683)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
