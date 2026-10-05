"""Export only public information, without importing the app or reading its data."""
from pathlib import Path
from shutil import copyfile
import re
from jinja2 import Environment, FileSystemLoader, select_autoescape

root = Path(__file__).resolve().parents[1]
docs = root/'docs'
(docs/'assets').mkdir(parents=True, exist_ok=True)
env = Environment(loader=FileSystemLoader(root/'templates'), autoescape=select_autoescape())
for page, title in [('privacy','Privatlivspolitik'),('terms','Brugsvilkår')]:
    html = env.get_template('legal.html').render(page=page, title=title)
    for source, target in [('/static/style.css','assets/style.css'),('/static/legal.css','assets/legal.css'),
        ('/static/icon.svg','assets/icon.svg'),('/static/logos/icon-180.png','assets/icon-180.png'),
        ('/privacy','privacy.html'),('/terms','terms.html'),('/static/site.webmanifest','assets/site.webmanifest')]:
        # Preserve cache versions while making URLs work on GitHub Pages.
        html = re.sub(r'href="' + re.escape(source) + r'(\?[^"<>]*)?"',
                      lambda match: 'href="' + target + (match.group(1) or '') + '"', html)
    html = html.replace('href="/"','href="https://github.com/qlerup/fjordbudget"').replace('Tilbage til appen →','FjordBudget på GitHub →').replace('Åbn FjordBudget','Se projektet')
    (docs/(page+'.html')).write_text(html,encoding='utf-8')
for source, target in [('style.css','style.css'),('legal.css','legal.css'),('icon.svg','icon.svg'),
                        ('logos/icon-180.png','icon-180.png'),('logos/social.png','social.png')]:
    copyfile(root/'static'/source,docs/'assets'/target)
(docs/'assets/site.webmanifest').write_text('{"name":"FjordBudget","theme_color":"#28533f"}',encoding='utf-8')
(docs/'.nojekyll').touch()
(docs/'index.html').write_text('''<!doctype html><html lang="da"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FjordBudget</title><link rel="stylesheet" href="assets/style.css"><link rel="stylesheet" href="assets/legal.css"></head><body class="legal-page"><main class="legal-content"><img src="assets/social.png" style="width:100%;height:auto" alt="FjordBudget — dit private økonomiske overblik"><h1>Din økonomi. Din installation.</h1><p>FjordBudget giver overblik over konti, posteringer og budget med læseadgang til banken. Installér gennem FjordHub og log ind med din FjordHub-konto.</p><p><a href="privacy.html">Privatlivspolitik</a> · <a href="terms.html">Brugsvilkår</a> · <a href="https://github.com/qlerup/fjordbudget">GitHub og installation</a></p><p>Denne hjemmeside indeholder kun information. Der indtastes ingen bankdata eller API-nøgler her.</p></main></body></html>''',encoding='utf-8')
