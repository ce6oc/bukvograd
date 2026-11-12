#!/usr/bin/env python3
"""Генератор аудиоклипов «Буквоград».

Читает фразы прямо из index.html (данные букв, наклейки, похвала), строит
полный инвентарь произносимых строк, синтезирует их голосом Piper
(ru_RU-irina-medium) и кладёт в audio/<fnv16>.m4a.

Имя файла — FNV-1a 32-бит (hex) от текста фразы. Тот же алгоритм реализован
в index.html (hash36), поэтому манифест соответствий на рантайме не нужен.

Запуск:  tools/.venv/bin/python tools/gen_audio.py [--force]
"""
import json
import re
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_PATH = ROOT / 'index.html'
OUT_DIR = ROOT / 'audio'
MODEL_PATH = Path(__file__).resolve().parent / 'models' / 'irina.onnx'
LIST_PATH = Path(__file__).resolve().parent / 'clips-list.json'

# Немного медленнее обычного (в приложении раньше был rate=.85)
LENGTH_SCALE = 1.12
AAC_BITRATE = '48k'


def fnv1a_hex(text: str) -> str:
    """FNV-1a 32bit по UTF-16 code units — точно как charCodeAt в index.html.

    Строки с эмодзи (TIPS) в JS дают два code unit на символ, поэтому
    нельзя считать по кодовым точкам Python.
    """
    raw = text.encode('utf-16-le')
    units = struct.unpack(f'<{len(raw) // 2}H', raw)
    h = 2166136261
    for cu in units:
        h ^= cu
        h = (h * 16777619) & 0xFFFFFFFF
    return format(h, 'x')


def parse_html():
    html = HTML_PATH.read_text(encoding='utf-8')
    letters = [
        {'ch': c, 'name': n, 'snd': s, 'word': w}
        for c, n, s, w in re.findall(
            r"ch:'([^']+)',name:'([^']+)',snd:'([^']+)',word:'([^']+)',emo:'[^']*'", html)]
    assert len(letters) == 33, f'ожидались 33 буквы, найдено {len(letters)}'
    stickers = re.findall(r"id:'[^']+',emo:'[^']+',name:'([^']+)',hint", html)
    assert len(stickers) == 9, f'ожидались 9 наклеек, найдено {len(stickers)}'
    praise_raw = re.search(r"PRAISE=\[([^\]]*)\]", html).group(1)
    praise = re.findall(r"'([^']+)'", praise_raw)
    assert len(praise) == 5, praise
    tips_raw = re.search(r"const TIPS=\[([^\]]*)\]", html).group(1)
    tips = re.findall(r"'([^']+)'", tips_raw)
    assert len(tips) == 6, tips
    return letters, stickers, praise, tips


def build_inventory(letters, stickers, praise, tips):
    """Все строки, которые приложение передаёт в speak() (аргумент sp / текст).

    Фразы с комбинациями («Это X. Ищем Y») в рантайме проигрываются
    последовательностью клипов-сегментов, поэтому генерируются сегменты.
    """
    T = set()
    T.update(praise)
    T.update(tips)  # тапы по Буквику на главной
    T |= {
        'Все пары нашлись!',
        'Не пара. Попробуй ещё!',
        'Все поймал! Ням!',
        'Молодец! Ты вспомнил буквы!',
        'Привет! Я Буквик. Давай учить буквы вместе!',
        'Найди пару: соедини большую и маленькую букву',
        'Соедини большие и маленькие буквы!',
        # Приветствия на главной (greetSpeak)
        'Привет! Я Буквик. Начнём с буквы А? Нажми на неё на карте!',
        'Ты сегодня уже большой молодец! Можно отдохнуть.',
        'Ура! Ты знаешь все буквы! Играем в микс!',
    }
    for L in letters:
        ch, name, snd, word = L['ch'], L['name'], L['snd'], L['word']
        low = ch.lower()
        starts = word[0] == low
        # Атрибуты и отдельные слова/звуки
        T.add(name)   # тап по букве в «парах»
        T.add(word)   # сегмент «это арбуз» при промахе
        T.add(snd)    # «как звучит»
        # Карточка буквы (вход с карты)
        T.add(f'Буква {name} уже твоя! Повторим?')
        T.add(f'Привет! Это буква {name}. Выбирай станцию!')
        T.add(f'Молодец! Буква {name} выучена!')
        T.add(f'С возвращением! Сегодня знакомимся с буквой {name}')  # главная
        # Приветствие знакомства (в рантайме «» убираются — тут сразу без них)
        if low == 'ь':
            hello = 'Я мягкий знак. Я не звучу, но делаю буквы мягкими: лось!'
        elif low == 'ъ':
            hello = 'Я твёрдый знак. Я не звучу, но разделяю слово: обезьяна!'
        else:
            tail = (f'{word[0].upper() + word[1:]} начинается с меня!'
                    if starts else f'Я живу в слове {word}!')
            hello = f'Привет! Я буква {ch}. Меня зовут {name}. {tail}'
        T.add(hello)
        T.add(f'Это буква {name}')          # тап по карточке буквы
        T.add(f'Меня зовут {name}')          # чип «как зовут»
        # тап по слову
        T.add(f'{word}. Слово начинается с буквы {name}' if starts
              else f'{word}. В этом слове живёт буква {name}')
        # Игра «найди букву»
        T.add(f'Где буква {ch}?')            # текст-заголовок (повтор до загрузки)
        T.add(f'Найди букву {name}')
        T.add(f'Это {name}.')                # сегмент промаха (и «поймай»)
        T.add(f'Ищем {name}')
        # Игра «в каком слове»
        T.add(f'В каком слове живёт буква {ch}?')
        T.add(f'В каком слове живёт буква {name}?')
        T.add(f'{word}! Здесь живёт буква {name}')
        T.add(f'Тут нет буквы {name}')
        # Игра «пары»
        T.add(f'Да! {name}')
        # Игра «поймай буквы»
        T.add(f'Лови буквы {ch}! Нужно 5 штук.')
        T.add(f'Лови букву {name}! Нужно 5 штук')
        T.add(f'Лови только {name}')
    for s in stickers:
        T.add(f'Ура! Новая наклейка: {s}!')
        T.add(f'Наклейка: {s}!')
    return sorted(T)


def main():
    force = '--force' in sys.argv
    OUT_DIR.mkdir(exist_ok=True)
    letters, stickers, praise, tips = parse_html()
    texts = build_inventory(letters, stickers, praise, tips)
    print(f'Инвентарь: {len(texts)} фраз')

    # Файлы, которые уже есть и не менялись — не пересинтезируем
    todo = []
    for t in texts:
        out = OUT_DIR / (fnv1a_hex(t) + '.m4a')
        if force or not out.exists():
            todo.append((t, out))

    if todo:
        from piper import PiperVoice
        from piper.config import SynthesisConfig
        voice = PiperVoice.load(str(MODEL_PATH))
        cfg = SynthesisConfig(length_scale=LENGTH_SCALE)
        for k, (text, out) in enumerate(todo, 1):
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as tmp:
                tmp_path = Path(tmp.name)
            with wave.open(str(tmp_path), 'wb') as wav:
                voice.synthesize_wav(text, wav, syn_config=cfg)
            subprocess.run(
                ['ffmpeg', '-y', '-loglevel', 'error', '-i', str(tmp_path),
                 '-ac', '1', '-ar', '22050', '-b:a', AAC_BITRATE, str(out)],
                check=True)
            tmp_path.unlink(missing_ok=True)
            if k % 50 == 0 or k == len(todo):
                print(f'  {k}/{len(todo)}')

    # Манифест для тестов/проверки (в приложение не попадает)
    items = [{'file': fnv1a_hex(t) + '.m4a', 'text': t} for t in texts]
    LIST_PATH.write_text(
        json.dumps(items, ensure_ascii=False, indent=1), encoding='utf-8')
    total = sum((OUT_DIR / (fnv1a_hex(t) + '.m4a')).stat().st_size for t in texts)
    print(f'Готово: {len(texts)} клипов, суммарно {total / 1e6:.1f} МБ')


if __name__ == '__main__':
    main()
