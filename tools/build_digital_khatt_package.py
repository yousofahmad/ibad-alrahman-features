#!/usr/bin/env python3
"""Build the audited optional Digital Khatt V2 package from official QUL exports.

Inputs are downloaded separately by an authorized developer. This tool never
logs in, downloads audio, changes fonts, or changes the embedded 1405 mushaf.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile

FORMAT_ID = 'digital_khatt_v2'
FONT_MODE = 'unicode_single'
PAGE_COUNT = 604
LINE_COUNT = 15
SOURCE_URLS = {
    'layout': 'https://qul.tarteel.ai/resources/mushaf-layout/21',
    'words': 'https://qul.tarteel.ai/resources/quran-script/48',
    'font': 'https://qul.tarteel.ai/resources/font/247',
    'font_license': 'https://openfontlicense.org/open-font-license-official-text/',
}
COPYRIGHT = 'Copyright (c) 2020-2024 Amine Anane, Copyright © 2024 Tarteel Inc.'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(value, pretty=False):
    return (json.dumps(value, ensure_ascii=False, sort_keys=pretty,
                       indent=2 if pretty else None,
                       separators=None if pretty else (',', ':')) + '\n').encode('utf-8')


def query(path, sql):
    connection = sqlite3.connect(f'file:{path.resolve()}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError(f'Invalid SQLite integrity: {path.name}')
        return [dict(row) for row in connection.execute(sql)]
    finally:
        connection.close()


def license_bytes(ofl_path):
    official = ofl_path.read_text(encoding='utf-8')
    marker = 'SIL OPEN FONT LICENSE Version 1.1 - 26 February 2007'
    if marker not in official or 'DISCLAIMER' not in official:
        raise ValueError('Expected the full official SIL OFL 1.1 text')
    # The official template's placeholder copyright/RFN header is replaced with
    # the unmodified downloaded font's actual embedded copyright notice.
    body = official[official.index(marker):].strip()
    return (COPYRIGHT + '\n\n'
            'DigitalKhatt New Madina / DigitalKhattV2.otf\n'
            'The original font is distributed byte-for-byte, without changes.\n'
            'This Font Software is licensed under the SIL Open Font License, Version 1.1.\n'
            'https://openfontlicense.org\n\n' + body + '\n\n'
            'RESOURCE ATTRIBUTION AND SEPARATE DATA STATUS\n'
            'Font: ' + SOURCE_URLS['font'] + '\n'
            'Quran word script: ' + SOURCE_URLS['words'] + '\n'
            'Mushaf layout: ' + SOURCE_URLS['layout'] + '\n'
            'Downloaded through the Quranic Universal Library (QUL), Tarteel.\n'
            'QUL displays no copyright information for the word script and layout.\n'
            'Their status is recorded as unknown, not as a prohibition or an OFL grant.\n'
            'The OFL above applies to the font only, not to the separate Quran datasets.\n'
            'Line fitting is performed by the app, not by Digital Khatt reference justification.\n').encode('utf-8')


def pagination(words, lines):
    if len(words) != 83668 or [row['id'] for row in words] != list(range(1, 83669)):
        raise ValueError('Expected all 83,668 canonical word/marker rows in order')
    if len({(row['surah'], row['ayah']) for row in words}) != 6236:
        raise ValueError('Expected all 6,236 canonical ayahs')
    word_pages = {}
    line_keys = set()
    page_numbers = set()
    previous_word = 0
    for line in lines:
        page, number = line['page_number'], line['line_number']
        if not isinstance(page, int) or not isinstance(number, int):
            raise ValueError('Non-integer page/line number')
        if not 1 <= page <= PAGE_COUNT or not 1 <= number <= LINE_COUNT:
            raise ValueError('Layout dimensions exceed the supported package')
        if (page, number) in line_keys:
            raise ValueError('Duplicate page/line row')
        line_keys.add((page, number))
        page_numbers.add(page)
        first, last = line['first_word_id'], line['last_word_id']
        if line['line_type'] != 'ayah':
            if isinstance(first, int) or isinstance(last, int):
                raise ValueError('Unexpected word membership in a non-ayah row')
            continue
        if not isinstance(first, int) or not isinstance(last, int) or first > last:
            raise ValueError('Invalid ayah word range')
        if first != previous_word + 1:
            raise ValueError('Gap, overlap or unordered canonical word range')
        previous_word = last
        for word_id in range(first, last + 1):
            if word_id in word_pages:
                raise ValueError('A canonical word occurs on more than one line')
            word_pages[word_id] = page
    if page_numbers != set(range(1, PAGE_COUNT + 1)):
        raise ValueError('Layout has missing pages')
    if set(word_pages) != {row['id'] for row in words}:
        raise ValueError('Layout does not place every canonical word exactly once')
    verses, word_index = {}, {}
    for word in words:
        key = f"{word['surah']}:{word['ayah']}"
        page = word_pages[word['id']]
        bounds = verses.setdefault(key, [page, page])
        bounds[0], bounds[1] = min(bounds[0], page), max(bounds[1], page)
        if word['word'] > 0:
            word_index.setdefault(key, {})[str(word['word'])] = page
    pages = []
    for page in range(1, PAGE_COUNT + 1):
        keys = sorted((key for key, span in verses.items() if span[0] <= page <= span[1]),
                      key=lambda key: tuple(map(int, key.split(':'))))
        if not keys:
            raise ValueError('Page contains no canonical ayah')
        pages.append({'page': page, 'data': [
            {'surah': int(key.split(':')[0]), 'start': int(key.split(':')[1]),
             'end': int(key.split(':')[1])} for key in keys]})
    return {'schema_version': 1, 'format_id': FORMAT_ID,
            'page_count': PAGE_COUNT, 'line_count': LINE_COUNT,
            'source': {'words': 'words.sqlite', 'layout': 'layout.sqlite',
                       'canonical_word_rows': len(words), 'canonical_ayah_count': len(verses),
                       'layout_rows': len(lines)},
            'pages': pages, 'verses': verses, 'words': word_index}


def write_zip(path, files):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(files.items()):
            if len(data) > 16 * 1024 * 1024:
                raise ValueError(f'Oversized package member: {name}')
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data, compresslevel=9)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('words', 'layout', 'font', 'canonical-words', 'ofl-text', 'output-repo'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    metadata_sql = 'SELECT id,location,surah,ayah,word FROM words ORDER BY id'
    words = query(args.words, metadata_sql)
    canonical = query(args.canonical_words, metadata_sql)
    if words != canonical:
        raise ValueError('Source word coordinates are not identical to the builtin canonical map')
    lines = query(args.layout, 'SELECT * FROM pages ORDER BY page_number,line_number')
    index = pagination(words, lines)
    index_data = encode(index)
    license_data = license_bytes(args.ofl_text)
    font_data = args.font.read_bytes()
    if font_data[:4] != b'OTTO':
        raise ValueError('Expected original OpenType Digital Khatt font')
    manifest = {
        'schema_version': 1, 'id': FORMAT_ID, 'page_count': PAGE_COUNT,
        'line_count': LINE_COUNT, 'font_mode': FONT_MODE,
        'pagination_matches_builtin': False, 'word_alignment_matches_builtin': True,
        'navigation_index_file': 'pagination.json',
        'navigation_index_sha256': digest(index_data), 'license_sha256': digest(license_data),
        'source_urls': SOURCE_URLS,
        'font_license': 'SIL Open Font License 1.1',
        'data_license_status': 'unknown_no_copyright_information',
    }
    files = {'manifest.json': encode(manifest, True), 'words.sqlite': args.words.read_bytes(),
             'layout.sqlite': args.layout.read_bytes(), 'fonts/quran.otf': font_data,
             'LICENSE.txt': license_data, 'pagination.json': index_data}
    repo = args.output_repo
    package = repo / 'mushaf' / 'formats' / (FORMAT_ID + '.zip')
    write_zip(package, files)
    entry = {
        'id': FORMAT_ID, 'name': 'مصحف المدينة بخط Digital Khatt V2',
        'description': 'صيغة اختيارية بخط Digital Khatt V2 الأصلي وتخطيطه ذي ١٥ سطرًا. '
                       'ضبط السطر بمحرك التطبيق وقد يختلف عن محرك Digital Khatt المرجعي. '
                       'الصيغة تجريبية ولا تغيّر المصحف الافتراضي.',
        'page_count': PAGE_COUNT, 'line_count': LINE_COUNT, 'has_tajweed': False,
        'font_mode': FONT_MODE, 'license_sha256': digest(license_data),
        'flags': ['public', 'unicode', 'experimental', 'disabled_by_default'],
        'compatibility_enabled': True, 'pagination_matches_builtin': False,
        'word_alignment_matches_builtin': True, 'navigation_index_file': 'pagination.json',
        'navigation_index_sha256': digest(index_data),
        'package_url': f'mushaf/formats/{FORMAT_ID}.zip',
        'sha256': digest(package.read_bytes()), 'size_bytes': package.stat().st_size,
        'source_url': SOURCE_URLS['layout'], 'font_source_url': SOURCE_URLS['font'],
        'word_script_source_url': SOURCE_URLS['words'],
        'font_license': 'SIL Open Font License 1.1',
        'data_license_status': 'unknown_no_copyright_information',
    }
    catalog_path = repo / 'mushaf' / 'catalog-v2.json'
    input_catalog = catalog_path if catalog_path.exists() else repo / 'mushaf' / 'catalog.json'
    catalog = json.loads(input_catalog.read_text(encoding='utf-8'))
    catalog['formats'] = [item for item in catalog['formats'] if item['id'] != FORMAT_ID] + [entry]
    catalog_path.write_bytes(encode(catalog, True))
    license_path = repo / 'mushaf' / 'licenses' / (FORMAT_ID + '.txt')
    license_path.parent.mkdir(parents=True, exist_ok=True)
    license_path.write_bytes(license_data)
    proof = {
        'schema_version': 1, 'format_id': FORMAT_ID, 'page_count': PAGE_COUNT,
        'line_count': LINE_COUNT, 'canonical_ayah_count': len(index['verses']),
        'canonical_word_rows': len(words), 'layout_rows': len(lines),
        'all_canonical_word_coordinates_identical': True,
        'all_canonical_words_placed_once': True,
        'all_pages_checked': list(range(1, PAGE_COUNT + 1)),
        'font_bytes_unmodified': True, 'font_sha256': digest(font_data),
        'source_sha256': {name: digest(files[member]) for name, member in
                          [('words', 'words.sqlite'), ('layout', 'layout.sqlite')]},
        'package_sha256': entry['sha256'], 'package_size_bytes': entry['size_bytes'],
        'pagination_sha256': digest(index_data), 'license_sha256': digest(license_data),
        'source_urls': SOURCE_URLS,
    }
    proof_path = repo / 'mushaf' / (FORMAT_ID + '_proof.json')
    proof_path.write_bytes(encode(proof, True))
    print(json.dumps({'package': str(package), 'bytes': entry['size_bytes'],
                      'sha256': entry['sha256'], 'proof': str(proof_path)}, indent=2))


if __name__ == '__main__':
    main()
