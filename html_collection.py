import pandas as pd
import requests
from bs4 import BeautifulSoup
from pathlib import Path
import random
import webbrowser
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options


def get_codes(csv_file: str) -> list[str]:
    df = pd.read_csv(csv_file, encoding='utf-8')
    # df['theme_name'] = [name.strip() for name in df['theme_name'].values]
    # df['FI_coverage'] = [name.strip() for name in df['FI_coverage'].values]
    return df['theme_code'].values


def get_html_by_requests(code: str) -> str:
    url = f'https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/jpp/F_TERM/ja/fTermList/fTermList{code}.html'
    dummy_user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
    response = requests.get(url, headers={"User-Agent": dummy_user_agent})

    # ステータスコードが200番台かどうか（存在するか）を判定
    if response.status_code == 200:
        soup = BeautifulSoup(response.content, "html.parser")
        html = soup.prettify()
        return html
    
    elif response.status_code == 404:
        print("存在しません（404 Not Found）")
    else:
        print(f"その他のステータスコード: {response.status_code}")
    return ''


def register(code: str):
    with open('files/done.txt', 'a') as f:
        f.write(f'\n{code}')
    print(f'確認済みリストに登録しました')

def save_html(html: str, code: str):
    if not html:
        return

    filename = f'files/{code}.html'
    with open(filename, 'w', encoding='utf-8') as f:
            f.write(html)
    print(f'ファイルを保存しました: {filename}')


def remove_finished(codes: list[str]) -> list[str]:
    with open('files/done.txt', 'r') as f:
        codes_finished = f.read().splitlines()
    print(f'{len(codes_finished)}件取得済み, ', end='')
    not_finished = list(set(codes) - set(codes_finished))
    return not_finished


def open_web(code: str):
    url = f'https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/jpp/F_TERM/ja/fTermList/fTermList{code}.html'
    webbrowser.open(url)


def get_html_by_selenium(code: str) -> str:
    options = Options()
    options.add_argument('--headless=new')
    # 必要に応じてウィンドウサイズを指定（レイアウト崩れ対策）
    options.add_argument('--window-size=1920,1080')

    # ドライバーの初期化
    # driver = webdriver.Chrome(options=options)
    driver = webdriver.Chrome()

    url = f'https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/jpp/F_TERM/ja/fTermList/fTermList{code}.html'
    print(url)
    driver.get(url)

    # ページ全体のHTMLを取得
    html_content = driver.page_source

    # ブラウザを閉じる
    driver.quit()
    return html_content


if __name__ == '__main__':
    codes = get_codes('files/code.csv')
    codes = remove_finished(codes)
    print(f'残り{len(codes)}件です')
    code = random.choice(codes)
    print(code)

    # html = get_html_requests(code)
    html = get_html_by_selenium(code)
    save_html(html, code)
    # open_web(code)

    register(code)

