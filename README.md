# EPD Image Resizer

사진을 e-paper 패널 크기로 맞추고 **16계조 그레이**로 변환하는 GUI 툴.
펌웨어와 무관하게 단독으로 동작합니다.

![](docs/screenshot.png)

## 실행

```bash
pip install -r requirements.txt     # Pillow 하나 (Tkinter는 Python 기본 포함)
python epd_image_tool.py
```

이미지 열기 → 패널 선택 → 회전/맞춤/밝기/대비/감마/디더링 조절 → 오른쪽 미리보기(실제 16계조 결과) 확인 → 저장.

## 패널 프리셋

| 패널 | 비트맵 (W × H) | 비고 |
|---|---|---|
| 6"  (OPM060E9)  | 1072 × 1448 | 세로형 — 가로 사진은 회전 90° |
| 7.8" (ED078KC2) | 1872 × 1404 | |
| 10.3" (ED103TC2) | 1872 × 1404 | |
| 사용자 정의 | 임의 (가로는 짝수) | |

## 출력

| 파일 | 형식 | 용도 |
|---|---|---|
| `.bmp` | 24-bit, R=G=B, 값 0,17,…,255 (16단계) | EPDdemo / EPDctrl 등 PC 툴 입력 |
| `.bin` | 4bpp packed, 1바이트에 2픽셀, **첫 픽셀이 상위 니블**, 0=검정 … 15=흰색 | IT8951 이미지 버퍼에 들어가는 바이트열 그대로 |

## 명령행 / 배치

```bash
python epd_image_tool.py --cli photo.jpg --panel 6 --rotate 90 --out photo.bmp --raw photo.bin
python epd_image_tool.py --cli photo.jpg --size 800x600 --fit fill --no-dither --out x.bmp
python epd_image_tool.py --selftest        # 프리셋별 크기·레벨·raw 길이·BMP 왕복 검증
```

옵션: `--panel 6|7.8|10.3`, `--size WxH`, `--rotate 0|90|180|270`, `--fit fit|fill|stretch`,
`--pad-black`, `--brightness`, `--contrast`, `--gamma`, `--no-dither`, `--invert`

## 변환 파이프라인

회전 → 맞춤(fit/fill/stretch) → 그레이 → 밝기·대비·감마 → 16계조 양자화(Floyd-Steinberg 디더링 선택) → 반전(선택)

디더링을 켜면 e-paper에서 계조 띠(banding)가 눈에 띄게 줄어듭니다. 선·텍스트 위주 이미지는 끄는 편이 깔끔합니다.
