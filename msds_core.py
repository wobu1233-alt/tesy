"""
MSDS 검토 도우미 - 서버(Python) 버전 핵심 로직

브라우저 버전(pdf.js)과 똑같은 방식으로 동작하되, 텍스트를 못 읽는 PDF(스캔본,
폰트 인코딩 깨진 문서)를 만나면 실제 OCR(Tesseract, 로컬 실행)로 자동 전환합니다.
브라우저 샌드박스와 달리 여기서는 외부 네트워크 제약이 없어서 OCR이 실제로 동작해요.
"""
import re
import json
import subprocess
import tempfile
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
with open(BASE_DIR / "cas_db.json", encoding="utf-8") as f:
    CAS_DB = json.load(f)

CAS_DB_BY_NUMBER = {entry["cas"]: entry for entry in CAS_DB}

CAS_RE = re.compile(r"\b(\d{2,7}-\d{2}-\d)\b")


def is_valid_cas(cas: str) -> bool:
    """CAS 체크섬 검증: 마지막 숫자가 앞자리들로 계산되는 검증 숫자와 일치하는지 확인."""
    m = re.match(r"^(\d{2,7})-(\d{2})-(\d)$", cas)
    if not m:
        return False
    digits = (m.group(1) + m.group(2))[::-1]
    check_digit = int(m.group(3))
    total = sum((i + 1) * int(d) for i, d in enumerate(digits))
    return total % 10 == check_digit


def match_cas(cas: str):
    return CAS_DB_BY_NUMBER.get(cas)


def _first_column(s: str) -> str:
    """레이아웃 보존 모드(pdftotext -layout)에서 값 뒤에 다른 열(전화번호 등)이
    공백 2칸 이상으로 이어붙는 경우가 많아, 첫 번째 열만 잘라 쓴다."""
    parts = re.split(r"\s{2,}", s.strip())
    return parts[0].strip() if parts and parts[0].strip() else s.strip()


def extract_msds_number(text: str) -> str:
    """1페이지 상단 등에 있는 'MSDS번호 : AA0000-0000000000' 값을 읽는다.
    라벨 없이 'AA00000-0000000000' 형태 코드만 단독으로 찍혀있는 문서도 있어서,
    라벨 매칭에 실패하면 그 형태를 곧장 찾는 걸로 한 번 더 시도한다. 국내 서식이 아닌
    해외 SDS는 'Reference number'를 그 대용으로 마지막에 시도한다. 없으면 '-'."""
    m = re.search(r"MSDS\s*번호\s*[:：]?\s*([A-Za-z0-9\-]+)", text)
    if m:
        val = m.group(1).strip()
        if val:
            return val
    m = re.search(r"[A-Z]{2}\d{4,6}-\d{8,12}", text)
    if m:
        return m.group(0)
    m = re.search(r"Reference\s*(?:No\.?|number)\s*[:：]?\s*([A-Za-z0-9\-]+)", text, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if val:
            return val
    return "-"


_MANUFACTURER_NOISE = {
    "자료없음", "해당없음", "정보", "업체정보", "업체 정보", "해당사항없음", "전화번호", "긴급전화번호",
    "회사명", "회사정보", "제조사", "제조자", "공급자", "공급사", "공급업체", "공급업체정보",
}


def _is_valid_manufacturer_value(cand: str) -> bool:
    if not cand or cand in _MANUFACTURER_NOISE:
        return False
    if cand.startswith("/"):  # 라벨이 슬래시로 병기된 경우 잘못 잘린 잔여 라벨 텍스트
        return False
    if re.match(r"^[가-하]\s*\.", cand):  # 'ㄱ.', '나.' 같은 다른 항목 번호로 잘못 걸린 경우
        return False
    if re.search(r"전화번호|긴급연락처|긴급전화|Emergency\s*telephone", cand, re.IGNORECASE):
        return False
    return True


def extract_manufacturer(text: str) -> str:
    """1번 항목의 '회사명'/'회사정보'/'제조사'/'제조자'/'공급자'/'공급업체' 값을 읽는다. 표 레이아웃이
    깨져서 값이 라벨 줄이 아니라 바로 위/아래 줄에 걸쳐있는 경우, 그리고 라벨 뒤에 '정보' 같은
    안내 문구만 있고 실제 값은 다음 줄에 있는 경우까지 대응한다. 없으면 '-'."""
    lines = text.splitlines()
    label_re = re.compile(
        r"^\s*(?:[-•·○○￮Ｏㅇ][.\s]*)?(?:(?:\d{1,2}|[가-하])[.\)]\s*)?"
        r"(회\s*사\s*명|회\s*사\s*정\s*보|제\s*조\s*사|제\s*조\s*자|공\s*급\s*자|공\s*급\s*사|공\s*급\s*업\s*체(?:\s*정\s*보)?)"
        r"(?=\s|[:：]|/|$)"
        r"\s*[:：]?[ \t]*(.*)$"
    )

    def line_value(line):
        """이 줄 자체가 또 다른 라벨 줄이면(예: '공급자' 헤더 다음 줄이 '회사명 ...'인 경우)
        그 라벨의 값만 뽑아 쓰고, 아니면 줄 원문을 그대로 후보로 쓴다."""
        m2 = label_re.match(line)
        raw = m2.group(2).strip() if m2 else line.strip()
        return _first_column(raw)

    def same_line_value(raw_group2: str) -> str:
        """라벨과 콜론 사이에 다른 라벨이 병기된 경우(예: '제조자/수입자/유통업자 정보 : 값'에서
        정규식이 '제조자'만 라벨로 인식해 '/수입자/유통업자 정보 : 값'이 그대로 남는 경우),
        진짜 값은 맨 마지막 콜론 뒤에 있으므로 그걸 우선한다."""
        if ":" in raw_group2 or "：" in raw_group2:
            raw_group2 = re.split(r"[:：]", raw_group2)[-1]
        return _first_column(raw_group2.strip(" :-"))

    for i, line in enumerate(lines):
        m = label_re.match(line)
        if not m:
            continue
        candidates = []
        same_line_val = same_line_value(m.group(2))
        if same_line_val:
            candidates.append(same_line_val)
        if i > 0:
            candidates.append(line_value(lines[i - 1]))
        if i + 1 < len(lines):
            candidates.append(line_value(lines[i + 1]))
        if i + 2 < len(lines):
            candidates.append(line_value(lines[i + 2]))
        for cand in candidates:
            cand = re.sub(r"^(제조사|제조자|공급사|공급자)\s*[:：]\s*", "", cand)
            if _is_valid_manufacturer_value(cand):
                return cand[:80]

    # 국내 서식 라벨이 전혀 없는 해외 SDS: "Supplier's details" / "Company" 계열을 시도한다.
    m = re.search(r"Supplier'?s?\s*details\s*[:：]?\s*([^\n]+)", text, re.IGNORECASE)
    if m:
        val = _first_column(m.group(1).strip(" :-"))
        if _is_valid_manufacturer_value(val):
            return val[:80]
    m = re.search(r"Company\s*(?:name)?\s*[:：]\s*([^\n]+)", text, re.IGNORECASE)
    if m:
        val = _first_column(m.group(1).strip(" :-"))
        if _is_valid_manufacturer_value(val):
            return val[:80]
    return "-"


_MONTH_DAY_YEAR_HINT_RE = re.compile(r"\(?\s*월\s*/\s*일\s*/\s*년\s*\)?")


def extract_last_revision_date(text: str) -> str:
    """16번 항목의 '최종 개정일자' 값을 읽는다. 'YYYY.MM.DD', 'YYYY-MM-DD', 'YYYY년 MM월 DD일',
    '10회/2024년02월20일'처럼 개정횟수와 한 줄에 붙어있는 경우, 그리고 '27-5-2024'처럼
    일-월-년 순서로 뒤집혀 있는 경우까지 폭넓게 인식한다. 16번에 없고 1페이지 상단
    ('개정일: ...')에만 있는 문서도 있어서, '최종' → '개정일자' → '개정일' 순으로 넓혀가며
    찾는다. 국내 서식이 전혀 아닌 해외 SDS는 'Date of revision' 계열을 마지막에 시도한다.
    문서 어딘가에 '(월/일/년)' 표기가 있으면(PPG 등 해외계 문서에서 흔함), 'X/Y/YYYY'처럼
    애매한 숫자 날짜를 일/월이 아니라 월/일 순서로 해석한다.
    없으면 '-'."""
    mdy_format = bool(_MONTH_DAY_YEAR_HINT_RE.search(text))
    for needle in ("최종", "개정일자", "개정일"):
        idx = text.find(needle)
        if idx == -1:
            continue
        window = text[idx:idx + 150]
        m = re.search(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?", window)
        if m:
            y, mo, d = m.groups()
            return f"{y}.{int(mo):02d}.{int(d):02d}"
        # 연도가 맨 앞이 아니라 '일-월-년'(국내) 또는 '월-일-년'(해외, 위 힌트가 있을 때) 순서로 찍힌 경우
        m = re.search(r"\b(\d{1,2})\s*[.\-/]\s*(\d{1,2})\s*[.\-/]\s*(\d{4})\b", window)
        if m:
            a, b, y = m.groups()
            mo, d = (a, b) if mdy_format else (b, a)
            return f"{y}.{int(mo):02d}.{int(d):02d}"
        # '3월 13 2023'처럼 월만 한글로 붙어있고 나머지는 공백으로만 구분된 경우
        m = re.search(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일?\s*,?\s*(\d{4})", window)
        if m:
            mo, d, y = m.groups()
            return f"{y}.{int(mo):02d}.{int(d):02d}"
    m = re.search(r"Date\s*of\s*(?:issue\s*/\s*)?revision\s*[:：]?\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})", text, re.IGNORECASE)
    if m:
        mo, d, y = m.groups()  # 영문 SDS는 통상 MM/DD/YYYY 표기
        return f"{y}.{int(mo):02d}.{int(d):02d}"
    return "-"


def extract_product_name(text: str) -> str:
    """1번 항목의 '가. 제품명'(또는 '가. 제품 정보') 값을 직접 읽는다. (국내 표준 서식)
    'ㄱ.' 라벨 없이 그냥 '제품명: ...'만 있는 문서, BASF 등 해외 본사 스타일 문서의
    페이지 상단 '제품: <이름>' 표기, 영문 SDS의 'GHS product identifier'/'Product name'
    표기까지 순서대로 시도한다. 전부 실패하면 빈 문자열(호출부에서 파일명으로 대체)."""
    patterns = [
        r"가\s*\.?\s*제\s*품\s*(?:명|정\s*보)\s*[:：]?\s*([^\n]+)",
        r"^\s*제\s*품\s*명\s*[:：]\s*([^\n]+)",
        r"^\s*제\s*품\s*[:：]\s*([^\n]+)",
        r"GHS\s*product\s*identifier\s*[:：]?\s*([^\n]+)",
        r"Identification\s*of\s*the\s*product\s*[:：]?\s*([^\n]+)",
        r"Product\s*name\s*[:：]\s*([^\n]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.MULTILINE | re.IGNORECASE)
        if m:
            val = _first_column(m.group(1).strip(" :-"))
            if val and val not in ("자료없음", "해당없음"):
                return val[:80]
    return ""


def extract_text_pdftotext(pdf_path: str) -> str:
    """1차 시도: poppler의 pdftotext (레이아웃 보존 + 일반 모드 둘 다 시도)."""
    for args in (["-layout"], []):
        try:
            result = subprocess.run(
                ["pdftotext", *args, pdf_path, "-"],
                capture_output=True, timeout=60
            )
            text = result.stdout.decode("utf-8", errors="ignore")
            if len(text.replace(" ", "").replace("\n", "")) >= 200:
                return text
        except Exception:
            continue
    return ""


def extract_text_ocr(pdf_path: str, max_pages: int = 20) -> str:
    """2차 시도: 스캔본이거나 폰트가 깨진 PDF는 페이지를 이미지로 렌더링 후 OCR.

    OCR이 느린 진짜 원인은 대부분 여기 있었다:
      1) 필요 없는 뒷페이지까지 통째로 렌더링(convert_from_path가 max_pages를 몰라서
         문서 전체를 다 그림). → first_page/last_page로 딱 필요한 만큼만 렌더링.
      2) 300dpi는 tesseract 한글 인식엔 과한 해상도. → 220dpi로 낮춰도 정확도 차이는
         거의 없고 렌더링·OCR 둘 다 확 빨라진다.
      3) 페이지 렌더링이 싱글스레드였음. → thread_count로 poppler를 병렬 렌더링.
      4) 필요한 항목(1번 회사정보, 3번 구성성분, 15/16번 규제·개정일자)을 이미 다 찾았으면
         나머지 페이지는 OCR할 필요가 없다. → 몇 페이지씩 묶어서 OCR하다가, 핵심 항목이
         전부 확보되면 그 시점에서 조기 종료한다.
    """
    from pdf2image import convert_from_path, pdfinfo_from_path
    import pytesseract

    DPI = 240
    BATCH = 3  # 이만큼씩 렌더링+OCR 하고, 필요한 정보가 다 모였는지 확인한다

    try:
        total_pages = pdfinfo_from_path(pdf_path).get("Pages", max_pages)
    except Exception:
        total_pages = max_pages
    last_page = min(max_pages, total_pages)

    def _has_enough(acc_text: str) -> bool:
        """'충분히 읽었다'는 판단을, 느슨한 키워드 매칭이 아니라 실제 파싱 함수로 직접 확인한다.
        (예전엔 '15.'/'법적 규제' 같은 절 제목만 보이면 멈췄는데, 16번 '최종 개정일자' 값은
        그 절 제목이 나온 바로 다음 페이지에 있는 경우가 많아서 값을 놓치기 전에 멈추는
        사고가 났었다. 실제 추출 함수가 값을 뽑아낼 수 있는지로 판단하면 이런 사고가 없다.)"""
        if not CAS_RE.search(acc_text):
            return False
        if not extract_product_name(acc_text):
            return False
        if extract_manufacturer(acc_text) in ("-", ""):
            return False
        if extract_last_revision_date(acc_text) in ("-", ""):
            return False
        return True

    text_parts = []
    page = 1
    while page <= last_page:
        batch_last = min(page + BATCH - 1, last_page)
        images = convert_from_path(
            pdf_path, dpi=DPI, first_page=page, last_page=batch_last, thread_count=2
        )
        for image in images:
            text_parts.append(pytesseract.image_to_string(image, lang="kor+eng", config="--psm 6"))
        page = batch_last + 1
        if _has_enough("\n".join(text_parts)):
            break

    return "\n".join(text_parts)


def normalize_cas_spacing(text: str) -> str:
    """일부 MSDS는 CAS 번호를 '75 - 28 - 5'처럼 하이픈 앞뒤에 공백을 넣어 표기한다.
    이러면 CAS 정규식이 아예 매칭되지 않아 구성성분표 전체를 놓치게 되므로,
    다른 처리를 하기 전에 미리 '75-28-5' 형태로 정규화한다.
    (날짜나 고시번호 같은 다른 하이픈 표기는 마지막 자리가 '정확히 한 자리 숫자'라는
    조건 때문에 영향받지 않는다.)"""
    return re.sub(r"(\d{2,7})\s*-\s*(\d{2})\s*-\s*(\d)\b", r"\1-\2-\3", text)


def extract_text(pdf_path: str) -> tuple[str, str]:
    """반환값: (텍스트, 추출방식 'pdftotext' | 'ocr')"""
    text = extract_text_pdftotext(pdf_path)
    if text:
        return normalize_cas_spacing(text), "pdftotext"
    text = extract_text_ocr(pdf_path)
    return normalize_cas_spacing(text), "ocr"


def find_composition_section(text: str) -> str:
    start = None
    for pat in [r"구성\s*성분의?\s*명칭", r"3\s*\.\s*구성"]:
        m = re.search(pat, text)
        if m:
            start = m.start()
            break
    if start is None:
        return text
    end_match = re.search(r"4\s*\.\s*응급조치|응급조치요령", text[start:])
    end = start + end_match.start() if end_match else start + 3000
    return text[start:end]


def first_column(s: str) -> str:
    """표에서 큰 공백(2칸 이상)으로 구분된 첫 번째 칸(화학물질명)만 꺼낸다.
    '관용명 및 이명(異名)' 같은 다음 칸은 버린다."""
    parts = [p.strip() for p in re.split(r"\s{2,}", s.strip()) if p.strip()]
    return parts[0] if parts else ""


def extract_name_from_block(block: str, cas: str) -> str:
    """구성성분 표는 보통 '성분명 ... 자료없음/관용명 ... CAS ... 함유량' 형태의 한 덩어리(줄바꿈 포함)로
    나오므로, 그 CAS 앞부분 텍스트를 성분명으로 간주하고 정리한다. 성분명이 다음 줄까지 걸쳐
    있는 경우(예: 'Siloxanes and\\nsilicones, dimethyl')도 뒤쪽에서 이어 붙인다."""
    idx = block.find(cas)
    if idx == -1:
        return ""
    before = block[:idx]
    before = re.split(r"자료없음|해당없음|N/?A", before)[0]
    name_lines = [line.strip() for line in before.splitlines() if line.strip()]
    if name_lines:
        # 첫 줄만 칸(화학물질명 | 관용명 등) 구조가 있을 수 있으므로 첫 칸만 취하고,
        # 그 아래 이어지는 줄들은 이름이 그냥 줄바꿈된 것이므로 그대로 둔다.
        name_lines[0] = first_column(name_lines[0])
        name_lines = [l for l in name_lines if l]
    if len(name_lines) > 1:
        # 마지막 줄(=CAS가 있는 바로 그 줄의 시작 부분)이 아주 짧으면, 실제 이름이 아니라
        # 관용명 칸이 새 나온 것일 가능성이 높으므로 제외한다 (예: '...A\n에폭시  25036-25-3...').
        filtered = [name_lines[0]] + [l for l in name_lines[1:] if len(l) > 4]
        if filtered:
            name_lines = filtered

    # CAS/함유량이 있는 줄 다음에 오는, 숫자가 없는 짧은 줄은 성분명이 줄바꿈된 것으로 보고 이어 붙인다
    after_lines = block[idx:].splitlines()[1:]
    for line in after_lines:
        line = line.strip()
        if not line:
            break
        if re.search(r"\d", line):
            break
        name_lines.append(line)

    name = " ".join(name_lines)
    name = re.sub(r"\s{2,}", " ", name).strip(" -:·")
    name = re.sub(r"^(화학물질명|성분명?|물질명|이명\(관용명\))\s*", "", name)
    return name[:80]


def extract_name_line_based(scoped: str, cas: str) -> str:
    """한 성분이 한 줄에 다 나오는 표(예: '이산화 티타늄   Titanium Dioxide   13463-67-7   10~15') 형식에서
    성분명을 뽑는다. 이름이 길어서 위/아래 줄로 걸쳐 있는 경우도 최대한 이어 붙인다."""
    lines = scoped.split("\n")
    for i, line in enumerate(lines):
        if cas not in line:
            continue
        idx = line.find(cas)
        before = line[:idx].strip()
        before = re.split(r"자료없음|해당없음|N/?A", before)[0].strip()
        before = first_column(before)

        # 이 줄 자체의 이름 칸이 너무 짧으면(관용명 한 단어 정도만 있는 경우 등), 위/아래 줄이
        # 모두 비어있지 않고 CAS가 없는 '이름이 위아래로 쪼개진' 패턴인지 먼저 확인한다.
        # 단, '카본블랙'처럼 정상적으로 짧은 한글 성분명(4자)까지 여기 걸려서, 함유량 범위가
        # 줄바꿈되며 생긴 '<1' 같은 잔여 줄을 이름으로 잘못 붙잡는 사고가 있었다. 위/아래 줄이
        # 실제로 글자(한글/영문)를 포함하고 있을 때만(=진짜 이름 조각일 때만) 이어 붙인다.
        if len(before) <= 2:
            prev_line = lines[i - 1].strip() if i > 0 else ""
            next_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
            prev_has_cas = any(is_valid_cas(c) for c in CAS_RE.findall(prev_line))
            next_has_cas = any(is_valid_cas(c) for c in CAS_RE.findall(next_line))
            prev_has_letters = bool(re.search(r"[가-힣A-Za-z]", prev_line))
            next_has_letters = bool(re.search(r"[가-힣A-Za-z]", next_line))
            if (prev_line and not prev_has_cas and prev_has_letters and len(prev_line) < 60
                    and next_line and not next_has_cas and next_has_letters and len(next_line) < 60):
                name = f"{prev_line} {next_line}"
                name = re.sub(r"\s{2,}", " ", name).strip(" -:·")
                return name[:80]

        looked_back = False
        if len(before) < 2:
            j = i - 1
            collected = []
            while j >= 0 and lines[j].strip() and not any(is_valid_cas(c) for c in CAS_RE.findall(lines[j])):
                collected.insert(0, first_column(lines[j].strip()))
                j -= 1
                if len(collected) >= 2:
                    break
            before = " ".join(c for c in collected if c)
            looked_back = True
        name = before
        if looked_back and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if nxt and not re.search(r"\d", nxt) and len(nxt) < 40:
                name = f"{name} {nxt}"
        name = re.sub(r"\s{2,}", " ", name).strip(" -:·")
        name = re.sub(r"^(화학물질명|성분명?|물질명|이명\(관용명\))\s*", "", name)
        return name[:80]
    return ""

def split_composition_blocks(scoped: str) -> list:
    """구성성분 구간을 표의 '행' 단위로 나눈다 (빈 줄 기준으로 성분마다 블록이 나뉘는 문서가 많음)."""
    blocks = re.split(r"\n\s*\n", scoped)
    return [b for b in blocks if b.strip()]


def find_pct_near(text: str, idx: int, cas_len: int) -> str:
    after = text[idx + cas_len: idx + cas_len + 220]
    # CAS 바로 뒤에 '자료없음'(식별번호 없음) 같은 자리표시자가 먼저 나오고 그 다음에 실제
    # 함유량 값이 오는 문서가 있으므로, 앞쪽 자리표시자는 건너뛰고 찾는다.
    after_skipped = re.sub(r"^(\s*(자료없음|해당없음|N/?A)\s*)+", "", after)
    # CAS 뒤에 '/ KE-32293'처럼 사내 제품코드가 슬래시로 덧붙는 경우가 있어 이것도 건너뛴다.
    after_skipped = re.sub(r"^\s*/\s*[A-Za-z0-9\-]+\s*", "", after_skipped)
    # 폭이 좁은 표는 '20 -' 까지 쓰고 그 다음 칸(<30)이 줄바꿈 후 다음 줄로 넘어가면서
    # 열 정렬을 맞추려고 공백을 수십 칸씩 채워 넣는 경우가 있다(예: '20 -\n            <30').
    # 그 공백이 원래 검색 범위(60자)를 넘어가 버려서 값을 놓쳤었다. 범위를 넉넉히 넓히고,
    # 매칭 시에는 연속 공백(줄바꿈 포함)을 한 칸으로 뭉쳐서 비교한다.
    after_collapsed = re.sub(r"\s+", " ", after_skipped)
    m = re.match(r"^\s*(\d{1,3}(?:\.\d+)?)\s*[~\-]\s*([<>≤≥]?)\s*(\d{1,3}(?:\.\d+)?)(?!\d*-\d)", after_collapsed)
    if m:
        op = {"<": "<", ">": ">", "≤": "≤", "≥": "≥", "": ""}.get(m.group(2), m.group(2))
        return f"{m.group(1)}~{op}{m.group(3)}"
    m = re.match(r"^\s*(\d{1,3}(?:\.\d+)?)\s*%", after_skipped)
    if m:
        return m.group(1)
    m = re.match(r"^\s*([<>≤≥]=?)\s*(\d{1,3}(?:\.\d+)?)", after_skipped)
    if m:
        op = {"<": "<", ">": ">", "<=": "≤", ">=": "≥", "≤": "≤", "≥": "≥"}.get(m.group(1), m.group(1))
        return f"{op}{m.group(2)}"
    m = re.match(r"^\s*(\d{1,3}(?:\.\d+)?)(?!\s*-\d)(?![\d.])", after_skipped)
    if m:
        return m.group(1)

    before = text[max(0, idx - 20): idx]
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*[~\-]\s*(\d{1,3}(?:\.\d+)?)\s*$", before)
    if m:
        return f"{m.group(1)}~{m.group(2)}"
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%\s*$", before)
    if m:
        return m.group(1)
    m = re.search(r"(?<!\d)(\d{1,3}(?:\.\d+)?)\s*$", before)
    if m:
        return m.group(1)
    return ""


def find_section15_text(text: str) -> str:
    m = re.search(r"15\s*\.\s*법적\s*규제", text)
    if not m:
        return ""
    start = m.start()
    end_m = re.search(r"16\s*\.\s*그\s*밖의|16\s*\.\s*참고", text[start:])
    end = start + end_m.start() if end_m else start + 4000
    return text[start:end]


def core_name(name: str) -> str:
    return re.split(r"[(（]", name or "")[0].strip()


def display_name(c: dict) -> str:
    """KOSHA 고시 별표21·22 표준명(DB 매칭명)을 기준 이름으로 보여주고,
    문서 원문 표기가 다르면 괄호로 같이 보여준다.
    DB의 표준명 자체에 영문 이명이 괄호로 붙어있는 경우(예: 'n-부틸 아세테이트(n-Butyl acetate)')는
    핵심 한글명만 기준 이름으로 쓰고, 문서 표기가 다르면 그 뒤에 괄호로 덧붙인다.
    예: sec-부틸알코올 문서 표기 -> DB 표준명 '2-부탄올' -> '2-부탄올(sec-부틸알코올)'로 표시."""
    doc_name = (c.get("name") or "").strip()
    std_name_full = (c.get("stdName") or "").strip()
    std_name = core_name(std_name_full)
    if not std_name:
        return doc_name or "(성분명 확인필요)"
    if not doc_name or _no_space(std_name) == _no_space(core_name(doc_name)):
        return std_name
    return f"{std_name}({doc_name})"


def find_category_block(section15: str, header_patterns: list) -> str:
    """'○ 작업환경측정물질 \\n - 해당됨 (... X) \\n - 해당됨 (... Y) \\n ○ 다음카테고리' 같은 표 형식에서,
    특정 카테고리 헤더 다음부터 다음 '○' 헤더(또는 다음 큰 항목) 전까지의 블록만 뽑는다."""
    for pat in header_patterns:
        m = re.search(pat, section15)
        if not m:
            continue
        start = m.end()
        next_o = section15.find("○", start)
        next_num = re.search(r"\n\s*[가-힣]\.\s", section15[start:])
        candidates = [x for x in [next_o, (start + next_num.start()) if next_num else -1] if x != -1]
        end = min(candidates) if candidates else start + 1500
        return section15[start:end]
    return ""


def _no_space(s: str) -> str:
    """공백 제거 비교용. 같은 문서 안에서도 '초산부틸'/'초산 부틸'처럼
    구간마다 띄어쓰기가 다르게 나오는 경우가 흔해서, 매칭 전에 공백을 없애 비교한다."""
    return re.sub(r"\s+", "", s or "")


def name_in_bullet_list(block: str, cn: str) -> bool:
    """블록 안에서 '해당됨 (... cn)' 또는 '[cn] : 해당됨' 같은 한 줄(bullet)에 cn이
    '해당됨'과 함께 나오는지 확인한다. 띄어쓰기 차이는 무시한다."""
    if not block or not cn:
        return False
    cn_ns = _no_space(cn)
    for line in block.splitlines():
        if cn_ns in _no_space(line) and re.search(r"해당\s*됨", line) and not re.search(r"해당\s*없음", line):
            return True
    return False


def apply_section15_hints(text: str, comps: list) -> list:
    section15 = find_section15_text(text)
    if not section15:
        for c in comps:
            c["workEnvDoc"] = None
            c["specialExamDoc"] = None
        return comps

    # '작업환경측정물질' vs '작업환경측정대상물질', '특수건강검진대상물질' vs '특수건강진단물질' 등
    # 문서마다 '대상'이 붙었다 안 붙었다 하는 표기 차이를 모두 허용한다.
    WORK_KEYWORD_RE = re.compile(r"작업환경측정(?:대상)?물질")
    SPECIAL_KEYWORD_RE = re.compile(r"특수건강(?:검진|진단)(?:대상)?물질")

    # 현행 KOSHA 표준 서식: '○ 작업환경측정물질' / '○ 특수건강검진대상물질' 헤더 아래
    # '- 해당됨 (1% 이상 함유한 X)' 식으로 나열되는 경우가 많다. 이 형태를 우선 시도한다.
    work_block_raw = find_category_block(section15, [r"작업환경측정(?:대상)?물질"])
    special_block_raw = find_category_block(section15, [r"특수건강(?:검진|진단)(?:대상)?물질"])
    # '해당됨/해당없음' 불릿 목록 서식이 아니면(예: 성분명 뒤에 바로 문구가 붙는 구식 서식),
    # 이 카테고리 블록을 신뢰하지 않고 아래의 옛 방식(성분명 주변 문맥 탐색)으로 넘어간다.
    work_block = work_block_raw if re.search(r"해당\s*(됨|없음)", work_block_raw or "") else ""
    special_block = special_block_raw if re.search(r"해당\s*(됨|없음)", special_block_raw or "") else ""

    for c in comps:
        cn = core_name(c.get("name", ""))
        if not cn or len(cn) < 2:
            c["workEnvDoc"] = None
            c["specialExamDoc"] = None
            continue

        if work_block:
            c["workEnvDoc"] = name_in_bullet_list(work_block, cn)
        else:
            c["workEnvDoc"] = None
        if special_block:
            c["specialExamDoc"] = name_in_bullet_list(special_block, cn)
        else:
            c["specialExamDoc"] = None

        # 위 표준 서식이 없는 문서(예: 성분별로 '작업환경측정물질'이라는 문구가 바로 따라붙는
        # 구식 서식)는 기존 방식대로 이름 주변 문맥에서 직접 찾는다.
        if not work_block and not special_block:
            search_from = 0
            found = False
            while True:
                idx = section15.find(cn, search_from)
                if idx == -1:
                    break
                found = True
                after = section15[idx + len(cn):]
                boundary_m = re.search(r"\n\s*(-\s*\[|○)", after)
                window_end = idx + len(cn) + (boundary_m.start() if boundary_m else 200)
                window = section15[idx: min(window_end, idx + 400)]
                window_ns = _no_space(window)
                if WORK_KEYWORD_RE.search(window_ns):
                    c["workEnvDoc"] = True
                elif c["workEnvDoc"] is not True and re.search(r"해당\s*없음|자료\s*없음", window):
                    c["workEnvDoc"] = False
                if SPECIAL_KEYWORD_RE.search(window_ns):
                    c["specialExamDoc"] = True
                elif c["specialExamDoc"] is not True and re.search(r"해당\s*없음|자료\s*없음", window):
                    c["specialExamDoc"] = False
                search_from = idx + len(cn)
            if not found:
                c["workEnvDoc"] = None
                c["specialExamDoc"] = None
    return comps


def extract_use(text: str) -> str:
    """1번 항목(제품의 권고 용도와 사용상의 제한)에서 '용도' 값만 뽑는다.
    '제품의 권고 용도 <값>' 한 줄로 나오는 문서, '물질/혼합물의 용도 : <값>'·'제품의 용도 : <값>'처럼
    라벨이 따로 있는 문서, '용도 : <값>' 형태로만 나오는 문서를 모두 시도한다.
    ('제품의 권고 용도'+'와' 로 이어지는 항목 제목(헤더) 줄은 값이 아니므로 제외한다.)
    '물질/혼합물의 용도'가 실제 화학물질 용도로 더 구체적이라 최우선으로 시도하고,
    없으면 '제품의 용도', 그다음 일반 '용도' 순으로 넘어간다."""
    for pat in [
        r"제품의\s*권고\s*용도(?!\s*와)\s*[:：]?\s*([^\n]+)",
        r"물질\s*/?\s*혼합물의\s*용도\s*[:：]\s*([^\n]+)",
        r"제품의\s*용도\s*[:：]\s*([^\n]+)",
        r"(?:^|\n)\s*-?\s*용도\s*[:：]\s*([^\n]+)",
        r"권고\s*용도(?!\s*와)\s*[:：]?\s*([^\n]+)",
    ]:
        for m in re.finditer(pat, text):
            val = m.group(1).strip()
            val = re.split(r"사용상의\s*제한|제조사|회사명|공급자", val)[0].strip(" :-와")
            if val and val not in ("자료없음", "해당없음") and not re.match(r"^\d", val):
                return val[:60]
    return ""


def extract_kv_row_composition(scoped: str) -> list:
    """'물질명 X' / 'CAS 번호 Y' / '함유량(%) Z' 처럼 라벨 뒤에 콜론(:) 없이
    공백만으로 값이 이어지는 표 형식을 인식한다. 성분이 하나뿐인 문서(라벨 한 줄 = 값 하나)와
    여러 성분이 옆으로 나열된 문서(라벨 한 줄에 값이 여러 개, 2칸 이상 공백으로 구분) 둘 다 지원한다.
    예1(단일 성분):
        물질명    시안화 은(SILVER CYANIDE)
        CAS 번호   506-64-9
        함유량(%)  100%
    예2(여러 성분, 옆으로 나열):
        성분      Nickel sulfamate   Water
        CAS NO.   13770-89-3         7732-18-5
        함유량(%)  60                 40
    """
    name_tokens = None
    cas_tokens = None
    pct_tokens = None

    for line in scoped.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        m = re.match(r"^(?:물질명|성분)\s+(\S.*)$", stripped)
        if m and name_tokens is None and not re.search(r"CAS|함유량|이명|관용명", m.group(1)):
            name_tokens = [t.strip() for t in re.split(r"\s{2,}|\t", m.group(1).strip()) if t.strip()]
            continue

        m = re.match(r"^CAS\s*(?:번호|NO\.?)\s+(\S.*)$", stripped, re.I)
        if m and cas_tokens is None:
            cas_tokens = [t.strip() for t in re.split(r"\s{2,}|\t", m.group(1).strip()) if t.strip()]
            continue

        m = re.match(r"^함유량\s*\(%\)\s+(\S.*)$", stripped)
        if m and pct_tokens is None:
            pct_tokens = [t.strip() for t in re.split(r"\s{2,}|\t", m.group(1).strip()) if t.strip()]
            continue

    if not cas_tokens:
        return []

    results = []
    for i, cas_raw in enumerate(cas_tokens):
        cas = re.sub(r"\s+", "", cas_raw)
        if not is_valid_cas(cas):
            continue
        name = ""
        if name_tokens:
            name = name_tokens[i].strip() if i < len(name_tokens) else name_tokens[0].strip()
        pct = ""
        if pct_tokens:
            pct_raw = pct_tokens[i] if i < len(pct_tokens) else (pct_tokens[0] if len(pct_tokens) == 1 else "")
            pct_m = re.search(r"[\d.]+", pct_raw)
            if pct_m:
                pct = pct_m.group(0)
        results.append({"name": name, "cas": cas, "pct": pct})
    return results


def extract_cas_first_composition(scoped: str) -> list:
    """독일계 SDS 소프트웨어(예: RELO 등)에서 흔한, '이름-CAS' 순서가 아니라
    CAS 번호가 줄 맨 앞에 오고 그 뒤에 이름과 함유량이 같은 줄에 나란히 오는 표 형식.
    예: '123-86-4 n-Butyl acetate                    20-25%'
    이름 칸 자체에 '<12,6%'처럼 %가 섞인 수식어가 들어있는 경우도 있어서, 그 줄에서
    %로 끝나는 마지막 값을 진짜 함유량으로 보고, 그 앞부분을 이름으로 취급한다."""
    pct_tail_re = re.compile(r"([<>≤≥]?\s*\d[\d.,]*\s*(?:[-~]\s*[<>≤≥]?\s*\d[\d.,]*)?)\s*%\s*$")
    results = []
    for line in scoped.splitlines():
        m = re.match(r"^\s*(\d{2,7}-\d{2}-\d)\s+(.+)$", line)
        if not m:
            continue
        cas, tail = m.group(1), m.group(2)
        if not is_valid_cas(cas):
            continue
        tail = tail.rstrip()
        pct_m = pct_tail_re.search(tail)
        if not pct_m:
            continue  # 함유량이 이 줄에 없으면(=이 서식이 아니면) 건너뛴다
        pct_raw = re.sub(r"\s+", "", pct_m.group(1)).replace(",", ".")
        name = tail[:pct_m.start()].strip()
        if "~" in pct_raw or "-" in pct_raw.lstrip("<>≤≥"):
            parts = re.split(r"[-~]", pct_raw)
            if len(parts) == 2 and all(parts):
                lo, hi = parts
                hi_op = ""
                if hi and hi[0] in "<>≤≥":
                    hi_op, hi = hi[0], hi[1:]
                pct = f"{lo.lstrip('<>≤≥')}~{hi_op}{hi}"
            else:
                pct = pct_raw
        else:
            pct = pct_raw
        results.append({"name": name, "cas": cas, "pct": pct})
    return results


def extract_labeled_composition_blocks(scoped: str) -> list:
    """국내 표(화학물질명 | CAS | 함유량) 대신, BASF 등 해외 본사 스타일 MSDS에서 흔한
    '물질명 한 줄 + 함량(W/W): ... + CAS번호: ...' 세로 나열 형식을 인식해서
    (성분명, CAS, 함유량) 목록을 뽑는다. 이 서식이 아니면 빈 리스트를 반환한다.
    예:
        1-methoxypropan-2-ol
        함량 (W/W): >= 15 % - < 20 %
        CAS번호: 107-98-2
        기존화학물질번호: KE-23379
    """
    if not re.search(r"CAS\s*번호\s*[:：]", scoped):
        return []
    results = []
    for block in re.split(r"\n\s*\n", scoped):
        cas_m = re.search(r"CAS\s*번호\s*[:：]\s*([0-9][0-9\- ]{6,15}[0-9])", block)
        if not cas_m:
            continue
        cas = re.sub(r"\s+", "", cas_m.group(1))
        if not is_valid_cas(cas):
            continue
        label_re = r"^(함량|CAS\s*번호|기존화학물질번호|추가목록번호|화학물질\s*안전번호)"
        name = ""
        for line in block.splitlines():
            line = line.strip()
            if not line or re.match(label_re, line):
                continue
            name = line
            break
        pct = ""
        pct_m = re.search(r"함량\s*\([^)]*\)\s*[:：]\s*(.+)", block)
        if pct_m:
            nums = re.findall(r"\d+(?:\.\d+)?", pct_m.group(1))
            if len(nums) >= 2:
                pct = f"{nums[0]}~{nums[1]}"
            elif len(nums) == 1:
                pct = nums[0]
        results.append({"name": name, "cas": cas, "pct": pct})
    return results


def parse_msds(text: str, filename: str) -> dict:
    scoped = find_composition_section(text)
    labeled_blocks = (
        extract_kv_row_composition(scoped)
        or extract_labeled_composition_blocks(scoped)
        or extract_cas_first_composition(scoped)
    )

    comps = []
    if labeled_blocks:
        # BASF류 세로 나열 서식: 이름/함유량을 문서에서 직접, 나머지는 DB 매칭으로 채운다.
        for item in labeled_blocks:
            cas = item["cas"]
            db_entry = match_cas(cas)
            doc_name = item["name"]
            name = doc_name or (db_entry["name"] if db_entry else "")
            comps.append({
                "name": name,
                "nameSource": "document" if doc_name else ("db" if db_entry else ""),
                "stdName": db_entry["name"] if db_entry else "",
                "cas": cas,
                "pct": item["pct"],
                "dbWorkEnv": db_entry["workEnv"] if db_entry else None,
                "dbSpecialExam": db_entry["specialExam"] if db_entry else None,
            })
    else:
        raw_matches = list(dict.fromkeys(CAS_RE.findall(scoped)))
        valid_cas = [c for c in raw_matches if is_valid_cas(c)]
        blocks = split_composition_blocks(scoped)

        for cas in valid_cas:
            db_entry = match_cas(cas)

            # 1) 문서 자체의 표에서 이 CAS의 성분명을 직접 추출 (여러 표 구조를 순서대로 시도)
            line_name = extract_name_line_based(scoped, cas)
            block_name = ""
            block_is_single_row = False
            for block in blocks:
                if cas in block:
                    block_cas_count = sum(1 for c in CAS_RE.findall(block) if is_valid_cas(c))
                    block_is_single_row = (block_cas_count == 1)
                    block_name = extract_name_from_block(block, cas)
                    break

            def looks_reasonable(n):
                # 표 전체가 그대로 딸려온 경우(너무 길거나 헤더 단어 포함)를 걸러낸다
                if not n or len(n) > 60:
                    return False
                if re.search(r"구성\s*성분|CAS\s*(No|번호)|함유량", n):
                    return False
                return bool(re.search(r"[가-힣A-Za-z]{2,}", n))

            # 빈 줄로 성분마다 블록이 깔끔하게 나뉘는 문서(예: DOWSIL류)는 블록 기반이 더 완전하고,
            # 빈 줄 없이 표 전체가 한 덩어리인 문서(예: MINRO-AL, CR13류)는 블록 기반이 표 전체를
            # 끌어와버리므로 줄 기반이 더 정확하다. 블록에 CAS가 하나만 들어있는지로 이를 구분한다.
            if block_is_single_row and looks_reasonable(block_name):
                doc_name = block_name
            elif looks_reasonable(line_name):
                doc_name = line_name
            elif looks_reasonable(block_name):
                doc_name = block_name
            else:
                doc_name = ""

            name = doc_name or (db_entry["name"] if db_entry else "")

            pct = ""
            search_from = 0
            while True:
                idx = scoped.find(cas, search_from)
                if idx == -1:
                    break
                found = find_pct_near(scoped, idx, len(cas))
                if found:
                    pct = found
                    break
                search_from = idx + len(cas)
            comps.append({
                "name": name,
                "nameSource": "document" if doc_name else ("db" if db_entry else ""),
                "stdName": db_entry["name"] if db_entry else "",
                "cas": cas,
                "pct": pct,
                "dbWorkEnv": db_entry["workEnv"] if db_entry else None,
                "dbSpecialExam": db_entry["specialExam"] if db_entry else None,
            })

    comps = apply_section15_hints(text, comps)

    return {
        "productName": extract_product_name(text) or "-",
        "use": extract_use(text),
        "comps": comps,
        "msdsNumber": extract_msds_number(text),
        "manufacturer": extract_manufacturer(text),
        "lastRevisionDate": extract_last_revision_date(text),
    }


def final_target_status(c: dict) -> tuple:
    """최종 대상 여부는 KOSHA 공공데이터 API > 문서 15번 자기선언 > 우리 CAS DB 순으로 우선한다.
    (API가 KOSHA 공식 최신 데이터라 가장 신뢰도가 높고, 그다음은 제조사가 직접 작성한 문서 15번,
    마지막으로 내장 DB는 API 조회를 안 했거나 KOSHA DB에 없는 물질에 대한 보조 수단으로만 쓰인다.)
    """
    api_work = c.get("apiWorkEnv")
    work = api_work if api_work is not None else c.get("workEnvDoc")
    if work is None:
        work = c.get("dbWorkEnv")

    api_special = c.get("apiSpecialExam")
    special = api_special if api_special is not None else c.get("specialExamDoc")
    if special is None:
        special = c.get("dbSpecialExam")

    return bool(work), bool(special)


def format_register_text(parsed: dict) -> str:
    """구글 스프레드시트에 그대로 붙여넣을 수 있는 서식.
    # 제품명
    - 화학물질명(함유량%)
    - 화학물질명(함유량%)

    측정 대상 : 화학물질명, 화학물질명
    특검 대상 : 화학물질명
    """
    lines = [f"# {parsed['productName']}"]
    work_targets = []
    special_targets = []
    for c in parsed["comps"]:
        name = display_name(c)
        pct = c.get("pct", "")
        lines.append(f"- {name}({pct}%)" if pct else f"- {name}")

        work, special = final_target_status(c)
        if work:
            work_targets.append(name)
        if special:
            special_targets.append(name)

    lines.append("")
    lines.append(f"측정 대상 : {', '.join(work_targets) if work_targets else '없음'}")
    lines.append(f"특검 대상 : {', '.join(special_targets) if special_targets else '없음'}")
    return "\n".join(lines)


def build_target_summary(results: list) -> dict:
    """전체 업로드된 제품들을 통틀어 측정 대상/특검 대상 물질을 취합한다 (중복 제거).
    반환값: {"work": [...], "special": [...]}. 각 항목은
    {"name": 표시명, "cas": CAS, "products": [이 물질이 들어있는 제품명들]} 형태이며 이름순 정렬됨."""
    work_map: dict = {}
    special_map: dict = {}
    for r in results:
        product_name = r.get("productName") or "(제품명 확인필요)"
        for c in r.get("comps", []):
            key = c.get("cas") or display_name(c)
            name = display_name(c)
            work, special = final_target_status(c)
            if work:
                entry = work_map.setdefault(key, {"name": name, "cas": c.get("cas") or "", "products": []})
                if product_name not in entry["products"]:
                    entry["products"].append(product_name)
            if special:
                entry = special_map.setdefault(key, {"name": name, "cas": c.get("cas") or "", "products": []})
                if product_name not in entry["products"]:
                    entry["products"].append(product_name)

    def to_list(m):
        return sorted(m.values(), key=lambda e: e["name"])

    return {"work": to_list(work_map), "special": to_list(special_map)}


def _conflict_check(db_val, doc_val, api_val):
    """db/문서/API 값 중 서로 다른 값이 섞여 있으면 True. None(확인불가/미조회)은 비교에서 제외."""
    vals = [v for v in (db_val, doc_val, api_val) if v is not None]
    return len(set(vals)) > 1


def _fmt_status(val, unknown_label):
    if val is None:
        return unknown_label
    return "대상" if val else "해당없음"


def collect_conflicts(results: list) -> list:
    """전체 업로드된 제품을 통틀어, DB·문서 15번·KOSHA API 판정이 서로 어긋나는 물질만 모은다.
    반환값: 각 항목이 {"product": 제품명, "name": 표시명, "cas": CAS,
                       "work_conflict": bool, "work_detail": str,
                       "special_conflict": bool, "special_detail": str} 인 리스트.
    (측정 또는 특검 둘 중 하나라도 불일치인 경우만 포함)"""
    out = []
    for r in results:
        product_name = r.get("productName") or "(제품명 확인필요)"
        for c in r.get("comps", []):
            db_work, db_special = c.get("dbWorkEnv"), c.get("dbSpecialExam")
            doc_work, doc_special = c.get("workEnvDoc"), c.get("specialExamDoc")
            api_work, api_special = c.get("apiWorkEnv"), c.get("apiSpecialExam")

            work_conflict = _conflict_check(db_work, doc_work, api_work)
            special_conflict = _conflict_check(db_special, doc_special, api_special)

            if not (work_conflict or special_conflict):
                continue

            def detail(db_val, doc_val, api_val):
                parts = [f"DB: {_fmt_status(db_val, '미등록')}", f"문서 15번: {_fmt_status(doc_val, '언급없음')}"]
                if api_val is not None:
                    parts.append(f"KOSHA API: {_fmt_status(api_val, '확인불가')}")
                return " / ".join(parts)

            out.append({
                "product": product_name,
                "name": display_name(c),
                "cas": c.get("cas") or "",
                "work_conflict": work_conflict,
                "work_detail": detail(db_work, doc_work, api_work),
                "special_conflict": special_conflict,
                "special_detail": detail(db_special, doc_special, api_special),
            })
    return out


_API_EXTRA_COLUMNS = [
    "관리대상유해물질",
    "특별관리물질",
    "공정안전보고서(PSM) 제출 대상물질",
    "노출기준설정물질",
    "허용기준설정물질",
    "금지물질",
    "허가대상물질",
]


def build_consolidated_rows(results: list) -> list:
    """여러 제품의 분석 결과를 하나의 표로 합친다.
    각 행은 다음 키를 가진 dict이다:
    화학물질명 (상품명), CAS No, 사용 용도, 작업환경측정 대상물질, 특수건강검진 대상물질,
    관리대상유해물질, 특별관리물질, 공정안전보고서(PSM) 제출 대상물질, 노출기준설정물질,
    허용기준설정물질, 금지물질, 허가대상물질.
    제품명 행은 '# 상품명'(CAS·대상여부 빈칸), 성분 행은 '- 성분명(퍼센트%)'(해당 CAS, 대상이면 'o' 아니면 'x') 형태다.
    뒤 7개 컬럼은 KOSHA 공공데이터 API 조회 결과(apiReg)가 있을 때만 채워지고,
    조회하지 않았거나 KOSHA DB에 없으면 '-'로 표시한다.
    같은 제품에 속한 모든 행(제품명 행 포함)에 그 제품의 '사용 용도'/'MSDS번호'/'제조사'/'최종개정일자'를
    동일하게 채운다."""
    rows = []
    for r in results:
        product_name = r.get("productName") or "(제품명 확인필요)"
        use = r.get("use") or ""
        meta = {
            "MSDS번호": r.get("msdsNumber") or "-",
            "제조사": r.get("manufacturer") or "-",
            "최종개정일자": r.get("lastRevisionDate") or "-",
        }

        product_row = {
            "화학물질명 (상품명)": f"# {product_name}",
            "CAS No": "",
            "사용 용도": use,
            **meta,
            "작업환경측정 대상물질": "",
            "특수건강검진 대상물질": "",
        }
        for col in _API_EXTRA_COLUMNS:
            product_row[col] = ""
        rows.append(product_row)

        for c in r.get("comps", []):
            name = display_name(c)
            pct = c.get("pct") or ""
            label = f"- {name}({pct}%)" if pct else f"- {name}"
            work, special = final_target_status(c)
            reg9 = c.get("apiReg")

            comp_row = {
                "화학물질명 (상품명)": label,
                "CAS No": c.get("cas") or "",
                "사용 용도": use,
                **meta,
                "작업환경측정 대상물질": "o" if work else "x",
                "특수건강검진 대상물질": "o" if special else "x",
            }
            for col in _API_EXTRA_COLUMNS:
                comp_row[col] = ("o" if reg9[col] else "x") if reg9 is not None else "-"
            rows.append(comp_row)
    return rows


# ─────────────────────────────────────────────
# KOSHA 공공데이터포털 오픈API (물질안전보건자료 chemlist) 연동
# ─────────────────────────────────────────────
KOSHA_LIST_ENDPOINT = "https://apis.data.go.kr/B552468/msdschem/getChemList"
KOSHA_DETAIL15_ENDPOINT = "https://apis.data.go.kr/B552468/msdschem/getChemDetail15"


def _kosha_request(url: str, params: dict, timeout: int = 10):
    """공통 요청 처리: 응답을 파싱해서 {"ok", "items", "raw", "error"} 형태로 반환."""
    import requests
    import xml.etree.ElementTree as ET

    try:
        resp = requests.get(url, params=params, timeout=timeout)
    except Exception as e:
        return {"ok": False, "items": [], "raw": "", "error": f"요청 실패(네트워크): {e}"}

    raw_text = resp.text

    if resp.status_code != 200:
        return {
            "ok": False,
            "items": [],
            "raw": raw_text,
            "error": f"HTTP {resp.status_code} 에러 — 아래 원본 응답 내용을 확인해주세요.",
        }

    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as e:
        return {"ok": False, "items": [], "raw": raw_text, "error": f"XML 파싱 실패: {e}"}

    # 공공데이터포털 공통 에러 포맷(OpenAPI_ServiceResponse) 확인
    err_msg = root.findtext(".//errMsg")
    if err_msg and err_msg != "NORMAL_SERVICE":
        auth_msg = root.findtext(".//returnAuthMsg") or ""
        return {"ok": False, "items": [], "raw": raw_text, "error": f"{err_msg} {auth_msg}"}

    # 신규 포맷(response/header/resultCode) 확인
    result_code = root.findtext(".//resultCode")
    result_msg = root.findtext(".//resultMsg")
    if result_code and result_code != "00":
        return {"ok": False, "items": [], "raw": raw_text, "error": f"API 에러 [{result_code}] {result_msg}"}

    items = []
    for item in root.findall(".//item"):
        entry = {child.tag: (child.text or "").strip() for child in item}
        if entry:
            items.append(entry)

    return {"ok": True, "items": items, "raw": raw_text, "error": None}


def lookup_kosha_api(api_key: str, search_word: str, search_cnd: str = "1", timeout: int = 10):
    """getChemList 호출: CAS/국문명 등으로 화학물질을 검색해서 chemId를 비롯한 식별정보를 조회한다.

    Parameters
    ----------
    api_key : 공공데이터포털에서 발급받은 인증키 (인코딩된 원문 그대로 넘겨도 됨)
    search_word : 검색어 (search_cnd에 맞는 값. 기본은 CAS No.)
    search_cnd : 검색구분 — "0":국문명, "1":CAS No, "2":UN No, "3":KE No, "4":EN No
    """
    import requests
    from urllib.parse import unquote

    # 공공데이터포털에서 발급되는 키는 이미 URL 인코딩된 상태로 오는 경우가 많다.
    # requests의 params는 값을 다시 인코딩하므로, 그대로 넘기면 %2F -> %252F 처럼
    # 이중 인코딩되어 400 Bad Request가 난다. 먼저 원래 형태로 풀어준 뒤 넘긴다.
    decoded_key = unquote(api_key)

    params = {
        "serviceKey": decoded_key,
        "searchWrd": search_word,
        "searchCnd": search_cnd,
        "numOfRows": "10",
        "pageNo": "1",
    }
    return _kosha_request(KOSHA_LIST_ENDPOINT, params, timeout=timeout)


def lookup_kosha_detail15(api_key: str, chem_id: str, timeout: int = 10):
    """getChemDetail15 호출: chemId로 '15. 법적 규제현황' 상세 항목을 조회한다."""
    from urllib.parse import unquote

    decoded_key = unquote(api_key)
    params = {
        "serviceKey": decoded_key,
        "chemId": chem_id,
    }
    return _kosha_request(KOSHA_DETAIL15_ENDPOINT, params, timeout=timeout)


# 산업안전보건법에 의한 규제(O02) 항목의 itemDetail 안에서 대상 여부를 판단하는 키워드
_API_WORK_RE = re.compile(r"작업환경측정(?:대상)?물질")
_API_SPECIAL_RE = re.compile(r"특수건강(?:검진|진단)(?:대상)?물질")


def parse_detail15_flags(detail_items: list) -> tuple:
    """getChemDetail15의 item 목록에서 '산업안전보건법에 의한 규제'(O02) 항목을 찾아
    작업환경측정/특수건강진단 대상 여부를 판정한다. 항목 자체가 없으면 (None, None)."""
    for item in detail_items:
        if item.get("msdsItemCode") == "O02":
            detail = item.get("itemDetail") or ""
            work = bool(_API_WORK_RE.search(detail))
            special = bool(_API_SPECIAL_RE.search(detail))
            return work, special
    return None, None


# 산업안전보건법(O02) 산하 9개 세부항목 — 엑셀 다운로드용 전체 판정
_O02_REG_COLUMNS = [
    ("작업환경측정대상물질", r"작업환경측정(?:대상)?물질"),
    ("관리대상유해물질", r"관리대상유해물질"),
    ("특수건강진단대상물질", r"특수건강(?:검진|진단)(?:대상)?물질"),
    ("특별관리물질", r"특별관리물질"),
    ("공정안전보고서(PSM) 제출 대상물질", r"공정안전보고서|PSM"),
    ("노출기준설정물질", r"노출기준설정물질"),
    ("허용기준설정물질", r"허용기준(?:설정물질| 이하 유지)"),
    ("금지물질", r"(?<!허가대상)금지물질"),
    ("허가대상물질", r"허가대상물질"),
]


def _o02_reg_flags(detail_items: list):
    """getChemDetail15의 item 목록에서 O02(산업안전보건법) 항목을 찾아
    9개 세부항목 각각의 대상 여부(bool)를 담은 dict를 반환한다.
    O02 항목 자체가 없으면 None."""
    for item in detail_items:
        if item.get("msdsItemCode") == "O02":
            detail = item.get("itemDetail") or ""
            return {name: bool(re.search(pattern, detail)) for name, pattern in _O02_REG_COLUMNS}
    return None


def kosha_api_check_cas(api_key: str, cas: str, cache: dict | None = None) -> tuple:
    """CAS 번호 하나에 대해 getChemList → getChemDetail15를 순서대로 호출해서
    (apiWork, apiSpecial, error, reg9) 를 반환한다. cache가 주어지면 같은 CAS는 재호출하지 않는다.
    error가 None이 아니면 API 조회 자체가 실패한 것(네트워크/키 문제 등)이고,
    error가 None인데 (None, None, None)이면 KOSHA DB에 해당 CAS가 없거나 15항목이 비어있는 것이다.
    reg9는 산업안전보건법 9개 세부항목 dict (없으면 None)."""
    if cache is not None and cas in cache:
        return cache[cas]

    list_result = lookup_kosha_api(api_key, cas, search_cnd="1")
    if list_result["error"]:
        result = (None, None, f"조회 실패: {list_result['error']}", None)
    elif not list_result["items"]:
        result = (None, None, None, None)  # KOSHA DB 미등재 (에러는 아님)
    else:
        chem_id = list_result["items"][0].get("chemId")
        detail_result = lookup_kosha_detail15(api_key, chem_id)
        if detail_result["error"]:
            result = (None, None, f"조회 실패: {detail_result['error']}", None)
        else:
            work, special = parse_detail15_flags(detail_result["items"])
            reg9 = _o02_reg_flags(detail_result["items"])
            result = (work, special, None, reg9)

    if cache is not None:
        cache[cas] = result
    return result


def apply_kosha_api_hints(
    api_key: str,
    comps: list,
    cache: dict | None = None,
    persistent_get=None,
    persistent_set=None,
) -> list:
    """comps 리스트에 apiWorkEnv / apiSpecialExam / apiError / apiReg(9개 세부항목 dict) 필드를 채워 넣는다.

    KOSHA API는 CAS 하나당 요청을 2번(getChemList → getChemDetail15) 순서대로 날려야 해서,
    성분이 여러 개인 제품은 이 호출이 직렬로 쌓이면 눈에 띄게 느려진다. 네트워크 대기가
    대부분이라 CPU 병목이 아니므로, 아직 캐시에 없는 CAS들만 모아 스레드풀로 동시에
    조회하고 나서 comps에 채워 넣는다. (API에 과부하를 주지 않게 동시 요청 수는 제한한다.)

    persistent_get/persistent_set이 주어지면(Supabase 캐시), 세션이 달라도 같은 CAS는
    재조회하지 않는다. 물/에탄올처럼 여러 제품에 공통으로 등장하는 성분이 많아서,
    이 DB 차원 캐시가 실제 체감 속도에 가장 크게 기여한다."""
    if cache is None:
        cache = {}

    todo = []
    seen = set()
    for c in comps:
        cas = c.get("cas")
        if not cas or not is_valid_cas(cas) or cas in cache or cas in seen:
            continue
        seen.add(cas)
        if persistent_get is not None:
            hit = persistent_get(cas)
            if hit is not None:
                cache[cas] = hit
                continue
        todo.append(cas)

    if todo:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=min(3, len(todo))) as pool:
            futures = {pool.submit(kosha_api_check_cas, api_key, cas, None): cas for cas in todo}
            for fut in as_completed(futures):
                cas = futures[fut]
                result = fut.result()
                cache[cas] = result
                # 네트워크/키 에러로 실패한 조회는 일시적일 수 있으니 DB에 남기지 않고,
                # 정상 조회(값이 있든 "KOSHA 미등재"든)만 캐시에 저장한다.
                if persistent_set is not None and result[2] is None:
                    persistent_set(cas, *result)

    for c in comps:
        cas = c.get("cas")
        if not cas or not is_valid_cas(cas):
            c["apiWorkEnv"] = None
            c["apiSpecialExam"] = None
            c["apiError"] = None
            c["apiReg"] = None
            continue
        work, special, error, reg9 = cache[cas]
        c["apiWorkEnv"] = work
        c["apiSpecialExam"] = special
        c["apiError"] = error
        c["apiReg"] = reg9
    return comps


# ─────────────────────────────────────────────
# KOSHA MSDS 웹사이트(msds.kosha.or.kr)의 15항 표 형식 그대로 재구성
# (금지물질 / 허가대상물질 / 관리대상유해물질 / ... 각 칸을 itemDetail 텍스트에서 키워드로 판정)
# ─────────────────────────────────────────────

# (열 이름, itemDetail 안에서 이 열을 "해당"으로 판정할 키워드/정규식)
_TABLE_SCHEMA = {
    "산업안전보건법": {
        "code": "O02",
        "columns": [
            ("금지물질", r"(?<!허가대상)금지물질"),
            ("허가대상물질", r"허가대상물질"),
            ("관리대상유해물질", r"관리대상유해물질"),
            ("특별관리물질", r"특별관리물질"),
            ("작업환경측정 대상 유해인자", r"작업환경측정(?:대상)?물질"),
            ("특수건강진단 대상 유해인자", r"특수건강(?:검진|진단)(?:대상)?물질"),
            ("노출기준설정물질", r"노출기준설정물질"),
            ("허용기준 이하 유지 대상 유해인자", r"허용기준(?:설정물질| 이하 유지)"),
            ("공정안전보고서(PSM) 제출 대상 유해·위험물질", r"공정안전보고서|PSM"),
            ("영업비밀 인정제외 물질", r"영업비밀"),
        ],
    },
    "화학물질관리법": {
        "code": "O04",
        "columns": [
            ("기존화학물질", r"기존화학물질"),
            ("인체급성유해성물질", r"인체급성유해성물질"),
            ("인체만성유해성물질", r"인체만성유해성물질"),
            ("생태유해성물질", r"(?<!인체)생태\s*유해성물질"),
            ("금지물질", r"(?<!허가)금지물질"),
            ("제한물질", r"제한물질"),
            ("허가물질", r"허가물질"),
            ("사고대비물질", r"사고대비물질"),
        ],
    },
    "화학물질의 등록 및 평가 등에 관한 법률": {
        "code": "O12",
        "columns": [
            ("기존화학물질", r"기존화학물질"),
            ("인체급성유해성물질", r"인체급성유해성물질"),
            ("인체만성유해성물질", r"인체만성유해성물질"),
            ("생태유해성물질", r"생태\s*유해성물질"),
            ("금지물질", r"(?<!허가)금지물질"),
            ("제한물질", r"제한물질"),
            ("허가물질", r"허가물질"),
            ("중점관리물질", r"중점관리물질"),
        ],
    },
}


def _extract_paren(text: str, label_re: str):
    """예: '작업환경측정대상물질 (측정주기 : 6개월)' 에서 '6개월' 부분만 뽑아낸다."""
    m = re.search(label_re + r"\s*\(([^)]*)\)", text)
    if not m:
        return None
    value = m.group(1).strip()
    # "측정주기 : 6개월" 처럼 안에 라벨이 또 붙어있으면 라벨은 떼고 값만 남긴다
    value = re.sub(r"^[^:：]*[:：]\s*", "", value).strip()
    return value or None


def build_detail15_tables(detail_items: list) -> dict:
    """getChemDetail15 결과를 KOSHA 웹사이트 15항 표와 같은 구조로 재구성한다.

    Returns
    -------
    dict: {
        "산업안전보건법": {"콜럼명": True/False, ...},
        ...,
        "위험물안전관리법": {"위험물": str|None, "지정수량": str|None},
    }
    각 법령 항목의 item이 아예 없으면 해당 법령 키 자체가 결과에 없다.
    """
    by_code = {item.get("msdsItemCode"): item for item in detail_items}
    result = {}

    for law_name, spec in _TABLE_SCHEMA.items():
        item = by_code.get(spec["code"])
        if item is None:
            continue
        detail = item.get("itemDetail") or ""
        row = {}
        for col_name, pattern in spec["columns"]:
            row[col_name] = bool(re.search(pattern, detail))
        result[law_name] = row

    # 작업환경측정/특수건강진단 주기 정보는 따로 뽑아서 부가정보로 첨부
    o02 = by_code.get("O02")
    if o02:
        detail = o02.get("itemDetail") or ""
        cycle_work = _extract_paren(detail, r"작업환경측정(?:대상)?물질")
        cycle_special = _extract_paren(detail, r"특수건강(?:검진|진단)(?:대상)?물질")
        result.setdefault("_meta", {})
        if cycle_work:
            result["_meta"]["측정주기"] = cycle_work
        if cycle_special:
            result["_meta"]["진단주기"] = cycle_special

    # 위험물안전관리법(O06)은 체크리스트가 아니라 분류명+지정수량 형태라 별도 처리
    o06 = by_code.get("O06")
    if o06:
        detail = (o06.get("itemDetail") or "").strip()
        if not detail or detail == "해당없음":
            result["위험물안전관리법"] = {"위험물": "해당없음", "지정수량": None}
        else:
            m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", detail)
            if m:
                result["위험물안전관리법"] = {"위험물": m.group(1).strip(), "지정수량": m.group(2).strip()}
            else:
                result["위험물안전관리법"] = {"위험물": detail, "지정수량": None}

    return result
