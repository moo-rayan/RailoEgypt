import asyncio
from pathlib import Path
import edge_tts

OUTPUT_DIR = Path("arabic_numbers_1_500")
VOICE = "ar-EG-SalmaNeural"  # صوت مصري أنثى
# ممكن تجرب: ar-EG-ShakirNeural لو متاح عندك

ones = {
    0: "",
    1: "واحد",
    2: "اتنين",
    3: "تلاتة",
    4: "أربعة",
    5: "خمسة",
    6: "ستة",
    7: "سبعة",
    8: "تمانية",
    9: "تسعة",
}

teens = {
    10: "عشرة",
    11: "حداشر",
    12: "اتناشر",
    13: "تلتاشر",
    14: "أربعتاشر",
    15: "خمستاشر",
    16: "ستاشر",
    17: "سبعتاشر",
    18: "تمنتاشر",
    19: "تسعتاشر",
}

tens = {
    20: "عشرين",
    30: "تلاتين",
    40: "أربعين",
    50: "خمسين",
    60: "ستين",
    70: "سبعين",
    80: "تمانين",
    90: "تسعين",
}

hundreds = {
    100: "مية",
    200: "ميتين",
    300: "تلتمية",
    400: "ربعمية",
    500: "خمسمية",
}


def number_to_egyptian(n: int) -> str:
    if n <= 0 or n > 500:
        raise ValueError("Number must be from 1 to 500")

    if n < 10:
        return ones[n]

    if 10 <= n <= 19:
        return teens[n]

    if n < 100:
        unit = n % 10
        ten = n - unit
        if unit == 0:
            return tens[ten]
        return f"{ones[unit]} و{tens[ten]}"

    if n in hundreds:
        return hundreds[n]

    h = (n // 100) * 100
    rest = n % 100
    return f"{hundreds[h]} و{number_to_egyptian(rest)}"


async def generate_audio(number: int):
    text = number_to_egyptian(number)
    file_path = OUTPUT_DIR / f"{number}.mp3"

    communicate = edge_tts.Communicate(
        text=text,
        voice=VOICE,
        rate="+0%",
        volume="+0%"
    )

    await communicate.save(str(file_path))
    print(f"{number}: {text} -> {file_path}")


async def main():
    OUTPUT_DIR.mkdir(exist_ok=True)

    for i in range(1, 501):
        await generate_audio(i)
        await asyncio.sleep(0.1)  # تهدئة بسيطة عشان الطلبات متتضغطش

    print("Done: generated 1 to 500 audio files.")


if __name__ == "__main__":
    asyncio.run(main())