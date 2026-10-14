"""Install provided PNG launcher icons and Internet permission; stdlib-only."""
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent.parent
flutter = root / 'mobile'
res = flutter / 'android' / 'app' / 'src' / 'main' / 'res'
if not res.exists():
    raise SystemExit('First run: cd mobile && flutter create --platforms=android --org com.nexusarena .')
for density, size in [('mdpi',48), ('hdpi',72), ('xhdpi',96), ('xxhdpi',144), ('xxxhdpi',192)]:
    target = res / f'mipmap-{density}' / 'ic_launcher.png'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(root / 'web' / 'icons' / f'icon-{size}.png', target)
manifest = flutter / 'android' / 'app' / 'src' / 'main' / 'AndroidManifest.xml'
content = manifest.read_text(encoding='utf-8')
if 'android.permission.INTERNET' not in content:
    content = content.replace('<application', '<uses-permission android:name="android.permission.INTERNET" />\n    <application', 1)
manifest.write_text(content, encoding='utf-8')
print('NEXUS launcher icon and INTERNET permission installed.')
