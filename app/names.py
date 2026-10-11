"""Full-name matching shared by OCR ranking and current-profile author display."""
import re
import unicodedata


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name)).casefold()


def latin_name_forms(name: str) -> set[str]:
    name = "".join(char for char in unicodedata.normalize("NFKD", name).casefold()
                   if not unicodedata.combining(char))
    parts = re.findall(r"[a-z]+", name)
    if not parts:
        return set()
    # Accept surname-first / surname-last and joined or hyphenated given names,
    # but never expand initials or guess a transliteration.
    return {"".join(parts), "".join(parts[1:] + parts[:1])}


def matches_name(candidate: str, personal_name: str | None, personal_pinyin: str | None) -> bool:
    if re.search(r"[\u3400-\u9fff]", candidate):
        return bool(personal_name and normalize_name(candidate) == normalize_name(personal_name))
    return bool(personal_pinyin and latin_name_forms(candidate) & latin_name_forms(personal_pinyin))
