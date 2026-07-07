#!/usr/bin/env python3
"""List AI / LLM brand logo names without using external networks.

draw.io's bundled shape libraries have no modern AI/LLM brand logos, so an
"LLM app architecture" can otherwise render as generic boxes.

This corporate build intentionally does NOT return CDN-backed image styles and
does NOT fetch SVGs. External icon CDNs are disabled to avoid diagrams that make
network requests when opened or exported.

  python3 aiicons.py --list
  python3 aiicons.py "openai"

Use ordinary local draw.io shapes instead, or add approved local SVG assets in a
future package revision.

Usage: python3 aiicons.py <query> [--limit N] [--variant color|mono|text]
                                  [--size PX] [--embed] [--json] [--list]
"""
import argparse
import json
import os
import re
import sys

MANIFEST = os.path.join(os.path.dirname(__file__), "..", "data", "lobe-icons.json")
_VARIANT = re.compile(r"-(?:color|text(?:-[a-z]{2})?|brand(?:-color)?)$")

# Common RAG/LLM data stores that lobe-icons lacks. Kept as local names only;
# this corporate build does not resolve them through simple-icons CDN.
_SUPPLEMENT = {
    "qdrant",
    "milvus",
    "supabase",
    "redis",
    "postgresql",
    "mongodb",
    "elasticsearch",
    "neo4j",
    "kafka",
    "clickhouse",
    "duckdb",
    "mysql",
    "sqlite",
    "cassandra",
    "snowflake",
    "databricks",
    "mariadb",
    "couchbase",
}


def families(icons):
    """base brand name -> set of its variant filenames (without .svg)."""
    fam = {}
    for name in icons:
        base = _VARIANT.sub("", name)
        fam.setdefault(base, set()).add(name)
    return fam


def squish(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def search(fam, query, limit):
    """Rank brand bases against the query (squished + per-token matching)."""
    q = squish(query)
    tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if t]
    scored = {}
    for base in fam:
        b = squish(base)
        s = 0
        if q and q == b:
            s = 100
        elif q and b.startswith(q):
            s = 60
        elif q and q in b:
            s = 40
        for t in tokens:
            if t == b:
                s = max(s, 90)
            elif len(t) >= 3 and b.startswith(t):
                s = max(s, 50)
            elif len(t) >= 3 and t in b:
                s = max(s, 30)
        if s:
            scored[base] = s
    return sorted(scored, key=lambda base: (-scored[base], base))[:limit]


def search_supplement(query):
    """Fall back to local supplement names (exact or substring match)."""
    q = squish(query)
    if not q:
        return None
    if q in _SUPPLEMENT:
        return q
    for brand in _SUPPLEMENT:
        if q in brand or brand in q:
            return brand
    return None


def pick_variant(base, variants, prefer):
    order = {"color": ["-color", "-brand-color", "", "-brand", "-text", "-text-cn"],
             "mono":  ["", "-brand", "-color", "-brand-color", "-text", "-text-cn"],
             "text":  ["-text", "-text-cn", "-brand", "-brand-color", "-color", ""]}[prefer]
    for suffix in order:
        cand = base + suffix
        if cand in variants:
            return cand
    return next(iter(sorted(variants)), None)


def main():
    ap = argparse.ArgumentParser(description="List AI/LLM brand names; external CDN icon resolution is disabled.")
    ap.add_argument("query", nargs="?", help='brand name, e.g. "openai" or "claude"')
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--variant", choices=["color", "mono", "text"], default="color")
    ap.add_argument("--size", type=int, default=48, help="cell width/height in px (icons are square)")
    ap.add_argument("--embed", action="store_true",
                    help="kept for CLI compatibility; no external fetch is performed in this corporate build")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true", help="list all brand names and exit")
    args = ap.parse_args()

    if not os.path.exists(MANIFEST):
        sys.exit(f"error: manifest not found at {MANIFEST}")
    manifest = json.load(open(MANIFEST, encoding="utf-8"))
    fam = families(manifest["icons"])

    if args.list:
        for base in sorted(set(fam) | _SUPPLEMENT):
            print(base)
        return
    if not args.query:
        ap.error("a query is required (or use --list)")

    matches = search(fam, args.query, args.limit)
    if matches:
        suggestions = [{"brand": base, "file": pick_variant(base, fam[base], args.variant)}
                       for base in matches]
    else:
        brand = search_supplement(args.query)
        suggestions = [{"brand": brand, "file": f"local-name:{brand}"}] if brand else []

    if args.json:
        print(json.dumps({
            "query": args.query,
            "status": "disabled",
            "reason": "External CDN icon resolution is disabled in this corporate build.",
            "suggestions": suggestions,
            "fallback": "Use local draw.io shapes such as rounded boxes, cylinders, cloud shapes, or shapesearch.py."
        }, indent=2, ensure_ascii=False))
    else:
        if suggestions:
            names = ", ".join(s["brand"] for s in suggestions)
            print(f"external CDN icon resolution is disabled; matched brand name(s): {names}", file=sys.stderr)
        else:
            print(f"external CDN icon resolution is disabled; no local brand name for {args.query!r}", file=sys.stderr)
        print("Use local draw.io shapes instead, e.g. rounded service boxes or cylinder database shapes.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
