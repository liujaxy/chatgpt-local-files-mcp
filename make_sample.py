"""Generate a visual-only challenge. The answer is never written as text."""
from pathlib import Path
import secrets
from PIL import Image, ImageDraw, ImageFont

BASE = Path(__file__).resolve().parent


def main():
    root = BASE / 'sample-project'
    figures = root / '资料' / '图片'
    figures.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGB', (1200, 900), 'white')
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 58)
    palette = ['#D32F2F', '#1976D2', '#388E3C', '#F9A825', '#7B1FA2', '#F57C00']
    rng = secrets.SystemRandom()
    rng.shuffle(palette)
    shapes = ['circle', 'square', 'triangle', 'diamond']
    rng.shuffle(shapes)
    positions = [(80, 80), (680, 80), (80, 490), (680, 490)]
    for (x, y), color, shape in zip(positions, palette, shapes):
        if shape == 'circle':
            draw.ellipse((x, y, x + 230, y + 230), fill=color)
        elif shape == 'square':
            draw.rectangle((x, y, x + 230, y + 230), fill=color)
        elif shape == 'triangle':
            draw.polygon([(x + 115, y), (x, y + 230), (x + 230, y + 230)], fill=color)
        else:
            draw.polygon([(x + 115, y), (x + 230, y + 115), (x + 115, y + 230), (x, y + 115)], fill=color)
        token = ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(5))
        draw.text((x, y + 250), token, fill='black', font=font)
    image.save(figures / 'visual-check.png')
    (root / '说明.md').write_text('# 项目读取测试\n\n请读取这份文件，并实际查看下面的图片。\n\n![验证图片](资料/图片/visual-check.png)\n\n请描述图形及其位置，并读出每个图形下方的标记。答案仅存在于图片像素中。\n', encoding='utf-8')
    (root / '资料' / '更新测试.md').write_text('# 更新测试\n\n当前内容：第一版。\n', encoding='utf-8')


if __name__ == '__main__':
    main()
