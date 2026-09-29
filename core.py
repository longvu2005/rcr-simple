"""RCR task and annotation validation shared by the server and importer."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

CASES = ("INDIVIDUAL", "GROUP", "DUAL", "RELATIONAL")
PREFIX = "then retrieve target images where "
SUBJECT_PREFIX = "Identify Subject 1 as "


class InputError(ValueError):
    pass


def validate_email(value: object, *, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise InputError("email is required")
        return None
    if not isinstance(value, str):
        raise InputError("email must be text")
    email = value.strip()
    if len(email) > 254 or email.count("@") != 1 or any(c.isspace() for c in email):
        raise InputError("invalid email address")
    local, domain = email.rsplit("@", 1)
    if (
        not local
        or len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        or len(domain) > 253
        or not all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in domain.split(".")
        )
    ):
        raise InputError("invalid email address")
    return local + "@" + domain.lower()


def phrase(value: object, label: str, limit: int = 800) -> str:
    if not isinstance(value, str):
        raise InputError(f"{label} must be text")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise InputError(f"{label} contains invalid Unicode") from exc
    value = re.sub(r"\s+", " ", value).strip(" .;,\t\n")
    if len(value) > limit:
        raise InputError(f"{label} is too long (maximum {limit} characters)")
    if any(ord(c) < 32 for c in value):
        raise InputError(f"{label} contains a control character")
    return value


def target_body(value: object) -> str:
    value = phrase(value, "TARGET")
    if value.lower().startswith(PREFIX):
        value = value[len(PREFIX) :].strip()
    return value


def image_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise InputError("image paths must be relative paths inside RCR_IMAGE_ROOT")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in (".", "..") for part in value.split("/")):
        raise InputError("unsafe image path")
    if path.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
        raise InputError("image must be JPEG, PNG, or WebP")
    return path.as_posix()


def boxes(value: object, label: str) -> list[dict]:
    if not isinstance(value, list) or len(value) > 100:
        raise InputError(f"{label} must be a list of at most 100 boxes")
    result = []
    seen = set()
    for box in value:
        if not isinstance(box, dict):
            raise InputError(f"{label} has an invalid box")
        identity = box.get("identity_id", box.get("label", ""))
        if type(identity) not in (str, int):
            raise InputError(f"{label}: identity_id must be text or an integer")
        identity = str(identity).strip()
        if not identity or len(identity) > 100 or identity in seen:
            raise InputError(f"{label} has an empty or duplicate identity")
        seen.add(identity)
        try:
            x, y, width, height = (float(box[k]) for k in ("x", "y", "width", "height"))
        except (ValueError, KeyError, TypeError) as exc:
            raise InputError(f"{label}: invalid coordinates") from exc
        if not (
            0 <= x < 1
            and 0 <= y < 1
            and 0 < width <= 1 - x + 1e-8
            and 0 < height <= 1 - y + 1e-8
        ):
            raise InputError(f"{label}: boxes must use normalized coordinates 0..1")
        result.append(dict(identity_id=identity, x=x, y=y, width=width, height=height))
    return result


def normalize_task(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise InputError("each task must be a JSON object")
    sample_id = phrase(raw.get("sample_id"), "sample_id", 200)
    query_id = phrase(raw.get("query_image_id"), "query_image_id", 200)
    target_id = phrase(raw.get("target_image_id"), "target_image_id", 200)
    if not sample_id or not query_id or not target_id:
        raise InputError("sample_id, query_image_id and target_image_id are required")
    case = raw.get("case_type") or "INDIVIDUAL"
    if case not in CASES:
        raise InputError(f"case_type must be one of {', '.join(CASES)}")
    qboxes = boxes(raw.get("query_boxes"), "query_boxes")
    tboxes = boxes(raw.get("target_boxes"), "target_boxes")
    candidate_ids = {b["identity_id"] for b in qboxes} & {
        b["identity_id"] for b in tboxes
    }
    if not candidate_ids:
        raise InputError("no identity has a box in both query and target")
    target_ids = raw.get("target_image_ids", raw.get("positive_image_ids", [target_id]))
    if (
        not isinstance(target_ids, list)
        or not target_ids
        or target_id not in target_ids
    ):
        raise InputError("target_image_ids must include target_image_id")
    target_ids = [phrase(x, "target_image_id", 200) for x in target_ids]
    if not all(target_ids) or len(set(target_ids)) != len(target_ids):
        raise InputError("target_image_ids contains empty or duplicate IDs")
    split = raw.get("split")
    if split is not None:
        split = phrase(split, "split", 30).upper()
    initial = raw.get("subjects", [])
    if not isinstance(initial, list):
        raise InputError("subjects must be a list")
    if initial:
        initial = normalize_annotation(
            {"case_type": case, "subjects": initial,
             "select_texts": [""] * (1 if case in CASES[:2] else 2)},
            sorted(candidate_ids), False,
        )["subjects"]
    imported = raw.get("imported_annotation")
    # Canonical dataset rows also carry completed text at the top level.
    # Never silently turn already labeled data into empty tasks.
    if imported is None and any(k in raw for k in ("final_desc", "final_change")):
        desc = raw.get("final_desc")
        if not isinstance(desc, str) or not desc.startswith(SUBJECT_PREFIX):
            raise InputError("final_desc must start with 'Identify Subject 1 as '")
        texts = desc[len(SUBJECT_PREFIX):].split(" and Subject 2 as ", 1)
        imported = {"case_type": case, "subjects": initial,
                    "select_texts": texts, "target_condition": raw.get("final_change")}
    if imported is not None:
        imported = normalize_annotation(imported, sorted(candidate_ids), True)
        case = imported["case_type"]
        initial = imported["subjects"]
    return dict(
        sample_id=sample_id,
        case_type=case,
        split=split,
        query_image_id=query_id,
        target_image_id=target_id,
        target_image_ids=target_ids,
        query_image_path=image_path(raw.get("query_image_path")),
        target_image_path=image_path(raw.get("target_image_path")),
        query_boxes=qboxes,
        target_boxes=tboxes,
        candidate_identity_ids=sorted(candidate_ids),
        initial_subjects=initial,
        imported_annotation=imported,
    )


def normalize_annotation(
    value: object, candidate_ids: list[str], complete: bool
) -> dict:
    if not isinstance(value, dict):
        raise InputError("annotation must be an object")
    case = value.get("case_type")
    if case not in CASES:
        raise InputError("choose one of the four RCR cases")
    subjects = value.get("subjects")
    if not isinstance(subjects, list) or len(subjects) > 2:
        raise InputError("subjects must be a list of one or two subjects")
    needed = 1 if case in ("INDIVIDUAL", "GROUP") else 2
    if len(subjects) != needed:
        raise InputError(f"{case} requires {needed} subject(s)")
    normalized_subjects, used = [], set()
    for position, subject in enumerate(subjects, 1):
        if (not isinstance(subject, dict) or type(subject.get("subject_id")) is not int
                or subject["subject_id"] != position):
            raise InputError("subject_id must be 1 then 2")
        ids = subject.get("identity_ids")
        if not isinstance(ids, list):
            raise InputError(f"Subject {position} needs an identity_ids list")
        if any(type(identity) not in (str, int) for identity in ids):
            raise InputError("identity_ids must contain only text or integers")
        ids = [str(i) for i in ids]
        if len(ids) != len(set(ids)) or any(
            i not in candidate_ids or i in used for i in ids
        ):
            raise InputError("an identity cannot be unknown or assigned twice")
        if complete and not ids:
            raise InputError(f"Subject {position} has no selected identity")
        used.update(ids)
        normalized_subjects.append(dict(subject_id=position, identity_ids=ids))
    if complete:
        count = len(normalized_subjects[0]["identity_ids"])
        if case == "INDIVIDUAL" and count != 1:
            raise InputError("INDIVIDUAL requires exactly one identity")
        if case == "GROUP" and count < 2:
            raise InputError("GROUP requires at least two identities in Subject 1")
    texts = value.get("select_texts")
    if not isinstance(texts, list) or len(texts) != needed:
        raise InputError(f"{case} requires {needed} SELECT text box(es)")
    texts = [phrase(t, f"SELECT Subject {i}", 500) for i, t in enumerate(texts, 1)]
    condition = target_body(value.get("target_condition", ""))
    if complete:
        if any(not t for t in texts) or not condition:
            raise InputError("fill every SELECT box and the TARGET box")
        if any(re.search(r"\bSubject\s*[12]\b", t, re.IGNORECASE) for t in texts):
            raise InputError(
                "SELECT boxes contain descriptions only; do not type Subject 1/2"
            )
        if not re.search(r"\bSubject\s*1\b", condition, re.IGNORECASE):
            raise InputError("TARGET must explicitly mention Subject 1")
        if needed == 2 and not re.search(r"\bSubject\s*2\b", condition, re.IGNORECASE):
            raise InputError("TARGET must explicitly mention Subject 2")
        references = re.findall(r"\bSubject\s*(\d+)\b", condition, re.IGNORECASE)
        if any(int(number) not in range(1, needed + 1) for number in references):
            raise InputError("TARGET mentions a Subject that does not exist in this case")
        if re.search(r"\[[^\]]+\]", " ".join(texts) + condition):
            raise InputError("replace all text placeholders before submitting")
    desc = SUBJECT_PREFIX + texts[0]
    if needed == 2:
        desc += " and Subject 2 as " + texts[1]
    change = PREFIX + condition
    result = dict(
        case_type=case,
        subjects=normalized_subjects,
        select_texts=texts,
        target_condition=condition,
    )
    if complete:
        result.update(
            final_desc=desc, final_change=change, final_instruction=f"{desc}; {change}."
        )
    return result


def parse_json_tasks(text: str) -> list[object]:
    if not isinstance(text, str) or len(text.encode("utf-8")) > 30_000_000:
        raise InputError("import must be a JSON array or JSONL under 30 MB")
    text = text.lstrip("\ufeff")
    if not text.strip():
        raise InputError("import is empty")
    try:
        if text.lstrip().startswith("["):
            rows = json.loads(text)
        else:
            rows = []
            for number, line in enumerate(text.splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise InputError(f"invalid JSONL at line {number}: {exc.msg}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"invalid JSON near line {exc.lineno}: {exc.msg}") from exc
    if not isinstance(rows, list) or not rows or len(rows) > 20_000:
        raise InputError("import must contain 1–20,000 task rows")
    return rows
