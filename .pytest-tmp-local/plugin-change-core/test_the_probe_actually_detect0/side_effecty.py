from pathlib import Path
Path(__file__).with_name('touched.txt').write_text('x', encoding='utf-8')
