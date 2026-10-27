"""축종별 설정 — 화면 탭, 질병, 방역대 반경, 위험지수 가중치.

화면 구조와 분석 함수는 축종과 무관하게 같고, 여기 값만 바뀐다.
가중치는 초기값(전문가 가정)이며, 과거 발생 데이터가 쌓이면
analysis.fit_weights() 결과로 교체한다.
"""
from __future__ import annotations

SPECIES = {
    "poultry": {
        "label": "가금 (닭·오리)",
        "label_en": "Poultry",
        "diseases_en": {"HPAI": "Highly pathogenic avian influenza", "LPAI": "Low pathogenic avian influenza",
                        "ND": "Newcastle disease", "FT": "Fowl typhoid", "IB": "Infectious bronchitis"},
        "diseases": {
            "HPAI": "고병원성 조류인플루엔자",
            "LPAI": "저병원성 조류인플루엔자",
            "ND": "뉴캣슬병",
            "FT": "가금티푸스",
            "IB": "닭전염성기관지염",
        },
        "main_disease": "HPAI",
        "season_start_month": 9,         # 9월~이듬해 8월 = 한 유행 시즌 (예: 2025/26)
        "link": {"days": 30, "km": 30},  # 같은 시즌 안에서 30일·30km 이내 선행 발생이면 '연쇄 가능'으로 본다
        "wild": "wild_bird",
        "radii_km": {"control": 3, "surveillance": 10},
        "subspecies": ["산란계", "육계", "토종닭", "종계", "오리", "메추리"],
        "weights": {
            "density": 0.20,          # 시군 농장 밀집도
            "large_share": 0.10,      # 대규모 농장 비중
            "wild_proximity": 0.30,   # 야생조류 검출·철새도래지 근접
            "recent_outbreak": 0.25,  # 최근 21일 발생
            "contact": 0.10,          # 차량 접촉량
            "weather": 0.05,          # 저온·고습
        },
    },
    "pig": {
        "label": "돼지",
        "label_en": "Swine",
        "diseases_en": {"ASF": "African swine fever", "FMD": "Foot-and-mouth disease", "CSF": "Classical swine fever",
                        "PRRS": "PRRS", "PED": "Porcine epidemic diarrhea"},
        "diseases": {
            "ASF": "아프리카돼지열병",
            "FMD": "구제역",
            "CSF": "돼지열병",
            "PRRS": "돼지생식기호흡기증후군",
            "PED": "돼지유행성설사",
        },
        "main_disease": "ASF",
        "season_start_month": 1,
        "link": {"days": 30, "km": 30},
        "wild": "wild_boar",
        "radii_km": {"control": 3, "surveillance": 10},
        "subspecies": ["일반돼지", "흑돼지", "멧돼지(사육)"],
        "weights": {
            "density": 0.20, "large_share": 0.10, "wild_proximity": 0.30,
            "recent_outbreak": 0.25, "contact": 0.15, "weather": 0.0,
        },
    },
    "cattle": {
        "label": "소",
        "label_en": "Cattle",
        "diseases_en": {"FMD": "Foot-and-mouth disease", "LSD": "Lumpy skin disease", "BRUCELLA": "Brucellosis",
                        "TB": "Tuberculosis", "BSE": "Bovine spongiform encephalopathy"},
        "diseases": {
            "FMD": "구제역",
            "LSD": "럼피스킨",
            "BRUCELLA": "브루셀라",
            "TB": "결핵",
            "BSE": "소해면상뇌증",
        },
        "main_disease": "FMD",
        "season_start_month": 1,
        "link": {"days": 21, "km": 20},
        "wild": None,
        "radii_km": {"control": 3, "surveillance": 10},
        "subspecies": ["한우", "육우", "젖소"],
        "weights": {
            "density": 0.20, "large_share": 0.10, "wild_proximity": 0.0,
            "recent_outbreak": 0.30, "contact": 0.20, "weather": 0.20,  # 럼피스킨: 흡혈곤충 활동
        },
    },
}

# 원천 데이터의 자유 텍스트를 표준 코드로 바꾸는 사전
DISEASE_ALIASES = {
    "HPAI": ["고병원성조류인플루엔자", "고병원성 조류인플루엔자", "고병원성AI", "HPAI", "H5N1", "H5N6", "H5N8"],
    "LPAI": ["저병원성조류인플루엔자", "저병원성 조류인플루엔자", "LPAI"],
    "ND": ["뉴캣슬", "뉴캐슬"],
    "FT": ["가금티푸스", "추백리"],
    "IB": ["전염성기관지염"],
    "ASF": ["아프리카돼지열병", "ASF"],
    "CSF": ["돼지열병"],
    "FMD": ["구제역", "FMD"],
    "LSD": ["럼피스킨", "LSD"],
    "BRUCELLA": ["브루셀라"],
    "TB": ["결핵"],
    "PRRS": ["생식기호흡기", "PRRS"],
    "PED": ["유행성설사", "PED"],
    "BSE": ["해면상뇌증", "BSE"],
}

SPECIES_ALIASES = {
    "poultry": ["닭", "산란계", "육계", "토종닭", "종계", "오리", "육용오리", "종오리", "메추리", "가금", "칠면조", "거위"],
    "pig": ["돼지", "양돈", "흑돼지", "종돈", "비육돈"],
    "cattle": ["소", "한우", "육우", "젖소", "한육우", "유우"],
    "wild_bird": ["야생조류", "철새", "분변"],
    "wild_boar": ["멧돼지 폐사체", "야생멧돼지"],
}


def disease_code(text: str | None) -> str | None:
    if not text:
        return None
    t = text.replace(" ", "")
    for code, names in DISEASE_ALIASES.items():
        if any(n.replace(" ", "") in t for n in names):
            return code
    return None


def species_code(text: str | None) -> str | None:
    if not text:
        return None
    # 더 구체적인 야생 키워드를 먼저 본다
    for code in ("wild_boar", "wild_bird", "poultry", "pig", "cattle"):
        if any(n in text for n in SPECIES_ALIASES[code]):
            return code
    return None


def species_for_disease(code: str | None) -> list[str]:
    return [sp for sp, c in SPECIES.items() if code in c["diseases"]]
