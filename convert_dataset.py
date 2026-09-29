"""Convert the supplied PIPA pair_data + samples to importable RCR JSONL.

Example:
  python convert_dataset.py --pair-data pair_data.json --samples samples.jsonl \
      --out import.jsonl --all-pairs
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from core import InputError, normalize_annotation, normalize_task, target_body, validate_email


def relative_image(url: str) -> str:
    # Label Studio uses /data/local-files/?d=Amazing/Datasets/PIPA/images/train/foo.jpg.
    raw = parse_qs(urlsplit(url).query).get("d", [urlsplit(url).path])[0]
    if "/images/" in raw:
        raw = raw.split("/images/", 1)[1]
    return raw.lstrip("/")


def selection_texts(final_desc: str, count: int) -> list[str]:
    prefix = "Identify Subject 1 as "
    if not final_desc.startswith(prefix):
        raise InputError("final_desc is not in the expected RCR format")
    descriptions = final_desc[len(prefix) :].strip(" .")
    if count == 2:
        parts = descriptions.split(" and Subject 2 as ", 1)
        if len(parts) != 2:
            raise InputError("two-subject final_desc needs 'and Subject 2 as'")
        return parts
    return [descriptions]


def build_rows(
    pair_data: dict, samples: list[dict], all_pairs: bool, existing_mode: str
) -> tuple[list[dict], dict]:
    images = {r["image_id"]: relative_image(r["url"]) for r in pair_data["images"]}
    boxes = {r["box_id"]: r for r in pair_data["boxes"]}
    pair_index = {
        (r["query_image_id"], r["target_image_id"]): r for r in pair_data["pairs"]
    }
    links = defaultdict(lambda: {"QUERY": [], "TARGET": []})
    for link in pair_data["pair_links"]:
        if link["box_id"] in boxes:
            links[link["pair_id"]][link["side"]].append(boxes[link["box_id"]])

    by_pair = defaultdict(list)
    for row in samples:
        by_pair[(row["query_image_id"], row["target_image_id"])].append(row)
    if all_pairs:
        pairs = pair_data["pairs"]
    else:
        pairs = [pair_index[key] for key in by_pair if key in pair_index]

    output, skipped = [], defaultdict(int)
    for pair in pairs:
        key = pair["query_image_id"], pair["target_image_id"]
        sample_rows = by_pair.get(key)
        if not sample_rows:
            if not all_pairs:
                continue
            sample_rows = [{"sample_id": pair["pair_id"], "case_type": "INDIVIDUAL"}]
        linked = links[pair["pair_id"]]
        # The pair_links table restricts boxes to the actual query/target pair.
        sides = {}
        for label in ("QUERY", "TARGET"):
            seen = set()
            sides[label] = []
            for box in linked[label]:
                identity = str(box["label"])
                if identity in seen:
                    continue
                seen.add(identity)
                sides[label].append(
                    {
                        "identity_id": identity,
                        **{k: box[k] for k in ("x", "y", "width", "height")},
                    }
                )
        for sample in sample_rows:
            row = {
                "sample_id": sample["sample_id"],
                "case_type": sample["case_type"],
                "split": pair.get("split"),
                "query_image_id": key[0],
                "target_image_id": key[1],
                "target_image_ids": sample.get(
                    "target_image_ids", sample.get("positive_image_ids", [key[1]])
                ),
                "query_image_path": images.get(key[0], ""),
                "target_image_path": images.get(key[1], ""),
                "query_boxes": sides["QUERY"],
                "target_boxes": sides["TARGET"],
                "subjects": sample.get("subjects", []),
            }
            try:
                normalized = normalize_task(row)
                if "final_desc" in sample and existing_mode == "submitted":
                    annotation = {
                        "case_type": sample["case_type"],
                        "subjects": sample["subjects"],
                        "select_texts": selection_texts(
                            sample["final_desc"], len(sample["subjects"])
                        ),
                        "target_condition": target_body(sample["final_change"]),
                    }
                    normalize_annotation(
                        annotation, normalized["candidate_identity_ids"], True
                    )
                    row["imported_annotation"] = annotation
                    email = validate_email(sample.get("annotator_email"))
                    if email is not None:
                        row["annotator_email"] = email
                output.append(row)
            except (InputError, KeyError) as exc:
                skipped[str(exc)] += 1
    for sample in samples:
        if (sample["query_image_id"], sample["target_image_id"]) not in pair_index:
            skipped["sample pair missing from pair_data"] += 1
    return output, dict(skipped)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare RCR task import from PIPA pair_data"
    )
    parser.add_argument("--pair-data", required=True, type=Path)
    parser.add_argument("--samples", type=Path, help="optional completed samples.jsonl")
    parser.add_argument(
        "--all-pairs",
        action="store_true",
        help="include pairs not present in samples as unlabeled tasks",
    )
    parser.add_argument(
        "--existing-mode",
        choices=["submitted", "unassigned"],
        default="submitted",
        help="how to import labeled samples",
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if not args.samples and not args.all_pairs:
        parser.error("provide --samples, --all-pairs, or both")
    pair_data = json.loads(args.pair_data.read_text(encoding="utf-8-sig"))
    samples = []
    if args.samples:
        with args.samples.open(encoding="utf-8-sig") as source:
            samples = [json.loads(line) for line in source if line.strip()]
    rows, skipped = build_rows(pair_data, samples, args.all_pairs, args.existing_mode)
    if not rows:
        parser.error(f"no valid rows; issues: {skipped}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as output:
        for row in rows:
            output.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(
        json.dumps({"written": len(rows), "skipped": skipped}, ensure_ascii=False),
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
