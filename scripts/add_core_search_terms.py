#!/usr/bin/env python3
"""Add search terms for the core topics in every language the world import uses.

`pipeline.core_topics` are the only topics searched in `topic_tier: core`
cities, which is every city the world expansion adds (2026-10-23). A locale with
no `search_terms` silently falls back to English (`pipeline.py`), which for an
Arabic, Thai or Russian-speaking city searches for what local groups do not
write. `build_queries` uses the first two terms of a locale, so two or three
well-chosen ones are the whole value.

Idempotent and additive: a locale already present for a topic is left alone.

    python scripts/add_core_search_terms.py           # dry run
    python scripts/add_core_search_terms.py --apply
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

TOPICS_YAML = Path(__file__).resolve().parent.parent / "config" / "topics.yaml"

TERMS: dict[str, dict[str, list[str]]] = {
    "senior": {
        "ar": ["نادي كبار السن", "جمعية المتقاعدين", "ملتقى كبار السن"],
        "vi": ["câu lạc bộ người cao tuổi", "hội người cao tuổi", "câu lạc bộ hưu trí"],
        "th": ["ชมรมผู้สูงอายุ", "ชมรมผู้เกษียณ", "กลุ่มผู้สูงอายุ"],
        "ms": ["kelab warga emas", "persatuan pesara", "komuniti warga emas"],
        "bn": ["প্রবীণ সংঘ", "অবসরপ্রাপ্তদের ক্লাব"],
        "ne": ["ज्येष्ठ नागरिक क्लब", "ज्येष्ठ नागरिक समाज"],
        "my": ["သက်ကြီးရွယ်အို အသင်း", "အငြိမ်းစား အသင်း"],
        "km": ["សមាគមមនុស្សចាស់", "ក្លឹបមនុស្សចាស់"],
        "lo": ["ສະມາຄົມຜູ້ສູງອາຍຸ", "ສະໂມສອນຜູ້ສູງອາຍຸ"],
        "mn": ["ахмадын холбоо", "ахмадын клуб"],
        "he": ["מועדון גמלאים", "מועדון ותיקים", "קהילת גמלאים"],
        "am": ["የአረጋውያን ማህበር", "የጡረተኞች ማህበር"],
        "sw": ["klabu ya wazee", "chama cha wastaafu"],
        "fa": ["کانون بازنشستگان", "باشگاه سالمندان"],
        "ka": ["ხანდაზმულთა კლუბი", "პენსიონერთა კლუბი"],
        "hy": ["տարեցների ակումբ", "թոշակառուների ակումբ"],
        "az": ["ahıllar klubu", "təqaüdçülər klubu"],
        "ru": ["клуб пенсионеров", "клуб для пожилых", "совет ветеранов"],
        "sq": ["klubi i pensionistëve", "shoqata e të moshuarve"],
        "mk": ["клуб на пензионери", "здружение на пензионери"],
        "bs": ["klub penzionera", "udruženje penzionera"],
        "sr": ["klub penzionera", "udruženje penzionera"],
        "hr": ["klub umirovljenika", "udruga umirovljenika"],
        "sl": ["društvo upokojencev", "klub upokojencev"],
        "so": ["ururka waayeelka", "naadiga waayeelka"],
        "is": ["félag eldri borgara", "eldri borgarar"],
        "bg": ["пенсионерски клуб", "клуб на пенсионера"],
        "el": ["ΚΑΠΗ", "σύλλογος συνταξιούχων", "λέσχη ηλικιωμένων"],
        "et": ["pensionäride klubi", "eakate klubi"],
        "lt": ["senjorų klubas", "pagyvenusių žmonių klubas"],
        "lv": ["senioru klubs", "pensionāru biedrība"],
        "no": ["pensjonistforening", "seniorsenter", "seniortreff"],
        "ro": ["club pensionari", "clubul seniorilor", "asociația pensionarilor"],
        "sk": ["klub dôchodcov", "klub seniorov"],
        "uk": ["клуб пенсіонерів", "клуб для літніх людей"],
        "ja": ["老人クラブ", "シニアサークル"],
        "ko": ["노인회", "시니어 동호회"],
        "zh": ["老年协会", "老年人俱乐部"],
        "tr": ["emekliler derneği", "yaşlılar kulübü"],
        "da": ["pensionistforening", "seniorklub"],
        "fi": ["eläkeläisyhdistys", "seniorikerho"],
        "sv": ["pensionärsförening", "seniorträff"],
    },
    "volunteering": {
        "ar": ["تطوع", "فريق تطوعي", "جمعية تطوعية"],
        "vi": ["tình nguyện", "nhóm tình nguyện", "câu lạc bộ tình nguyện"],
        "th": ["อาสาสมัคร", "กลุ่มอาสาสมัคร", "ชมรมจิตอาสา"],
        "ms": ["sukarelawan", "kumpulan sukarelawan", "pertubuhan sukarela"],
        "bn": ["স্বেচ্ছাসেবী সংগঠন", "স্বেচ্ছাসেবক দল"],
        "ne": ["स्वयंसेवक समूह", "स्वयंसेवा"],
        "my": ["စေတနာ့ဝန်ထမ်း အဖွဲ့", "လူမှုကူညီရေး အသင်း"],
        "km": ["ក្រុមស្ម័គ្រចិត្ត", "អ្នកស្ម័គ្រចិត្ត"],
        "lo": ["ອາສາສະໝັກ", "ກຸ່ມອາສາສະໝັກ"],
        "mn": ["сайн дурын бүлэг", "сайн дурын ажил"],
        "he": ["התנדבות", "קבוצת מתנדבים", "ארגון מתנדבים"],
        "am": ["በጎ ፈቃደኞች ማህበር", "የበጎ ፈቃድ አገልግሎት"],
        "sw": ["kujitolea", "kikundi cha kujitolea"],
        "fa": ["گروه داوطلبان", "کار داوطلبانه"],
        "ka": ["მოხალისეები", "მოხალისეთა ჯგუფი"],
        "hy": ["կամավորներ", "կամավորական խումբ"],
        "az": ["könüllülər", "könüllü qrup"],
        "ru": ["волонтёры", "волонтерская организация", "волонтерский центр"],
        "sq": ["vullnetarë", "grup vullnetarësh"],
        "mk": ["волонтери", "волонтерска организација"],
        "bs": ["volonteri", "volonterska organizacija"],
        "sr": ["volonteri", "volonterski centar"],
        "hr": ["volonteri", "volonterski centar"],
        "sl": ["prostovoljci", "prostovoljsko društvo"],
        "so": ["tabarucayaal", "kooxda tabarucayaasha"],
        "is": ["sjálfboðaliðar", "sjálfboðastarf"],
        "bg": ["доброволци", "доброволческа организация"],
        "el": ["εθελοντές", "εθελοντική ομάδα"],
        "et": ["vabatahtlikud", "vabatahtlik töö"],
        "lt": ["savanoriai", "savanorių organizacija"],
        "lv": ["brīvprātīgie", "brīvprātīgo organizācija"],
        "no": ["frivillig", "frivilligsentral"],
        "ro": ["voluntariat", "grup de voluntari"],
        "sk": ["dobrovoľníci", "dobrovoľnícke centrum"],
        "uk": ["волонтери", "волонтерська організація"],
    },
    "choir": {
        "ar": ["كورال", "جوقة", "فرقة كورال"],
        "vi": ["dàn hợp xướng", "câu lạc bộ hợp xướng"],
        "th": ["คณะนักร้องประสานเสียง", "ชมรมขับร้องประสานเสียง"],
        "ms": ["koir", "kumpulan koir"],
        "bn": ["কোরাস দল", "গানের দল"],
        "ne": ["गायन समूह", "कोरस"],
        "my": ["သံပြိုင်အဖွဲ့", "သီချင်းအဖွဲ့"],
        "km": ["ក្រុមចម្រៀង", "ក្រុមចម្រៀងរួម"],
        "lo": ["ວົງຮ້ອງເພງ", "ກຸ່ມຮ້ອງເພງ"],
        "mn": ["найрал дуу", "дуучдын бүлэг"],
        "he": ["מקהלה", "מקהלת חובבים"],
        "am": ["የመዘምራን ቡድን", "መዘምራን"],
        "sw": ["kwaya", "kikundi cha kwaya"],
        "fa": ["گروه کر", "گروه آواز"],
        "ka": ["გუნდი", "საგუნდო კოლექტივი"],
        "hy": ["երգչախումբ", "երգչախմբի համույթ"],
        "az": ["xor", "xor kollektivi"],
        "ru": ["хор", "хоровой коллектив", "хоровая студия"],
        "sq": ["kor", "grup korali"],
        "mk": ["хор", "хорски ансамбл"],
        "bs": ["hor", "pjevački zbor"],
        "sr": ["hor", "pevački hor"],
        "hr": ["zbor", "pjevački zbor"],
        "sl": ["pevski zbor", "zbor"],
        "so": ["kooxda heesaha", "kooral"],
        "is": ["kór", "kórastarf"],
        "bg": ["хор", "хорова формация"],
        "el": ["χορωδία", "ερασιτεχνική χορωδία"],
        "et": ["koor", "segakoor"],
        "lt": ["choras", "chorinis kolektyvas"],
        "lv": ["koris", "jauktais koris"],
        "no": ["kor", "korforening"],
        "ro": ["cor", "grup coral"],
        "sk": ["spevácky zbor", "zbor"],
        "uk": ["хор", "хоровий колектив"],
    },
    "religion": {
        "vi": ["giáo xứ", "nhóm cầu nguyện", "cộng đoàn tôn giáo"],
        "th": ["ชุมชนศาสนา", "กลุ่มศึกษาพระคัมภีร์", "ชมรมพุทธศาสนา"],
        "ms": ["komuniti agama", "kumpulan usrah", "jemaah"],
        "bn": ["ধর্মীয় সংগঠন", "প্রার্থনা দল"],
        "ne": ["धार्मिक समूह", "सत्संग"],
        "my": ["ဘာသာရေး အသင်း", "ဓမ္မ အသင်း"],
        "km": ["សហគមន៍សាសនា", "ក្រុមអធិស្ឋាន"],
        "lo": ["ຊຸມຊົນສາສະໜາ", "ກຸ່ມສາສະໜາ"],
        "mn": ["шашны бүлэг", "сүм хийдийн бүлэг"],
        "he": ["קהילה דתית", "קהילת בית כנסת"],
        "am": ["የሃይማኖት ማህበር", "ሰንበት ትምህርት ቤት"],
        "sw": ["kikundi cha maombi", "jumuiya ya kidini"],
        "fa": ["گروه مذهبی", "هیئت مذهبی"],
        "ka": ["სამრევლო", "რელიგიური თემი"],
        "hy": ["եկեղեցական համայնք", "կրոնական խումբ"],
        "az": ["dini icma", "dini qrup"],
        "sq": ["bashkësi fetare", "grup lutjeje"],
        "mk": ["верска заедница", "парохија"],
        "bs": ["vjerska zajednica", "džemat"],
        "sr": ["verska zajednica", "parohija"],
        "hr": ["vjerska zajednica", "župna zajednica"],
        "sl": ["verska skupnost", "župnijska skupnost"],
        "so": ["jaaliyadda diinta", "xalqada quraanka"],
        "is": ["söfnuður", "trúfélag"],
        "bg": ["религиозна общност", "енорийска общност"],
        "el": ["ενορία", "θρησκευτική κοινότητα"],
        "et": ["kogudus", "usukogudus"],
        "lt": ["parapijos bendruomenė", "religinė bendruomenė"],
        "lv": ["draudze", "reliģiskā kopiena"],
        "no": ["menighet", "trossamfunn"],
        "ro": ["comunitate religioasă", "parohie"],
        "sk": ["náboženské spoločenstvo", "farnosť"],
        "uk": ["релігійна громада", "парафія"],
    },
    "dance": {
        "ar": ["نادي رقص", "دروس رقص", "فرقة رقص"],
        "vi": ["câu lạc bộ khiêu vũ", "nhóm nhảy", "lớp khiêu vũ"],
        "th": ["ชมรมเต้นรำ", "กลุ่มเต้น", "คลาสเต้นรำ"],
        "ms": ["kelab tarian", "kumpulan tarian"],
        "bn": ["নৃত্য দল", "নাচের ক্লাব"],
        "ne": ["नृत्य समूह", "डान्स क्लब"],
        "my": ["အကအဖွဲ့", "ကခုန်အသင်း"],
        "km": ["ក្លឹបរាំ", "ក្រុមរាំ"],
        "lo": ["ກຸ່ມເຕັ້ນ", "ສະໂມສອນເຕັ້ນລຳ"],
        "mn": ["бүжгийн клуб", "бүжгийн бүлэг"],
        "he": ["חוג ריקוד", "ריקודי עם", "להקת מחול"],
        "am": ["የዳንስ ቡድን", "የዳንስ ክለብ"],
        "sw": ["kikundi cha densi", "klabu ya densi"],
        "fa": ["کلاس رقص", "گروه رقص"],
        "ka": ["ცეკვის ჯგუფი", "ცეკვის კლუბი"],
        "hy": ["պարային խումբ", "պարի ակումբ"],
        "az": ["rəqs qrupu", "rəqs klubu"],
        "ru": ["танцевальный клуб", "танцевальная студия", "клуб бальных танцев"],
        "sq": ["grup vallëzimi", "klub vallëzimi"],
        "mk": ["танцов клуб", "фолклорен ансамбл"],
        "bs": ["plesni klub", "folklorni ansambl"],
        "sr": ["plesni klub", "folklorni ansambl"],
        "hr": ["plesni klub", "folklorni ansambl"],
        "sl": ["plesni klub", "folklorna skupina"],
        "so": ["kooxda qoob ka ciyaarka", "naadiga qoob ka ciyaarka"],
        "is": ["dansfélag", "dansklúbbur"],
        "bg": ["танцов клуб", "клуб за народни танци"],
        "el": ["χορευτικός σύλλογος", "σχολή χορού"],
        "et": ["tantsuklubi", "rahvatantsurühm"],
        "lt": ["šokių klubas", "šokių kolektyvas"],
        "lv": ["deju klubs", "deju kolektīvs"],
        "no": ["dansegruppe", "danseklubb"],
        "ro": ["club de dans", "ansamblu de dansuri"],
        "sk": ["tanečný klub", "folklórny súbor"],
        "uk": ["танцювальний клуб", "танцювальна студія"],
    },
    "fitness": {
        "ar": ["مجموعة لياقة بدنية", "نادي رياضي", "تمارين جماعية"],
        "vi": ["nhóm thể dục", "câu lạc bộ thể hình", "nhóm tập luyện"],
        "th": ["กลุ่มออกกำลังกาย", "ชมรมฟิตเนส"],
        "ms": ["kumpulan kecergasan", "kelab kecergasan"],
        "bn": ["ফিটনেস গ্রুপ", "ব্যায়াম দল"],
        "ne": ["फिटनेस समूह", "योग समूह"],
        "my": ["ကိုယ်လက်လေ့ကျင့်ရေး အဖွဲ့", "ဖစ်နက်စ် အသင်း"],
        "km": ["ក្រុមហាត់ប្រាណ", "ក្លឹបហាត់ប្រាណ"],
        "lo": ["ກຸ່ມອອກກຳລັງກາຍ", "ສະໂມສອນອອກກຳລັງກາຍ"],
        "mn": ["фитнесс бүлэг", "дасгалын бүлэг"],
        "he": ["קבוצת כושר", "אימון קבוצתי"],
        "am": ["የአካል ብቃት ቡድን", "የስፖርት ክለብ"],
        "sw": ["kikundi cha mazoezi", "klabu ya mazoezi"],
        "fa": ["گروه ورزشی", "باشگاه بدنسازی"],
        "ka": ["ფიტნეს ჯგუფი", "ვარჯიშის ჯგუფი"],
        "hy": ["ֆիթնես խումբ", "մարզական ակումբ"],
        "az": ["fitnes qrupu", "idman qrupu"],
        "ru": ["фитнес группа", "групповые тренировки", "беговой клуб"],
        "sq": ["grup fitnesi", "stërvitje në grup"],
        "mk": ["фитнес група", "групни тренинзи"],
        "bs": ["fitness grupa", "grupni treninzi"],
        "sr": ["fitnes grupa", "grupni treninzi"],
        "hr": ["fitness grupa", "grupni treninzi"],
        "sl": ["vadba v skupini", "fitnes skupina"],
        "so": ["kooxda jimicsiga", "naadiga jimicsiga"],
        "is": ["hóptímar", "líkamsræktarhópur"],
        "bg": ["фитнес група", "групови тренировки"],
        "el": ["ομάδα γυμναστικής", "ομαδική προπόνηση"],
        "et": ["treeningrühm", "rühmatreening"],
        "lt": ["sporto klubas", "grupinės treniruotės"],
        "lv": ["fitnesa grupa", "grupu nodarbības"],
        "no": ["treningsgruppe", "gruppetrening"],
        "ro": ["grup de fitness", "antrenament de grup"],
        "sk": ["fitness skupina", "skupinové cvičenie"],
        "uk": ["фітнес група", "групові тренування"],
    },
}


def add_terms(text: str) -> tuple[str, int]:
    added = 0
    for topic, by_locale in TERMS.items():
        m = re.search(rf"^  - name: {re.escape(topic)}\n    search_terms:\n((?:      .+\n)+)",
                      text, re.M)
        if not m:
            raise SystemExit(f"topic {topic!r} not found in {TOPICS_YAML}")
        block = m.group(1)
        have = set(re.findall(r"^      ([a-z-]+):", block, re.M))
        lines = [f"      {loc}: [{', '.join(json.dumps(t, ensure_ascii=False) for t in terms)}]\n"
                 for loc, terms in by_locale.items() if loc not in have]
        added += len(lines)
        text = text[:m.end(1)] + "".join(lines) + text[m.end(1):]
    return text, added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    text, added = add_terms(TOPICS_YAML.read_text(encoding="utf-8"))
    print(f"{added} locale entries to add")
    if args.apply and added:
        TOPICS_YAML.write_text(text, encoding="utf-8")
        print(f"wrote {TOPICS_YAML}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
