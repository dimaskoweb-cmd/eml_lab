# -*- coding: utf-8 -*-
"""
publish_to_github.py — публикует index.html и library.json в GitHub-репозиторий
(GitHub Pages) через GitHub API, без установки git на машину и без git push.

Как работает:
  1. Берёт токен из файла .github_token (в этой же папке, в .gitignore — в репозиторий
     не попадает).
  2. Через Git Data API (blobs → tree → commit → ref) собирает один коммит из
     index.html и library.json и отправляет его напрямую в ветку main репозитория.
     Contents API (простой PUT) здесь не годится — у него лимит ~1 МБ на файл,
     а library.json обычно больше.

Настройка (один раз):
  1. GitHub → Settings → Developer settings → Personal access tokens →
     Fine-grained tokens → New token. Repository access: только этот репозиторий
     (eml_lab). Permissions → Contents: Read and write.
  2. Скопируй токен и сохрани его в файл .github_token в этой папке (без переносов
     строк, только сам токен). Ничего больше в этот файл не пиши.

Запуск: двойной клик по publish.bat (после run_parser.bat), либо
  python publish_to_github.py
"""

import base64
import os
import sys

try:
    import requests
except ImportError:
    print("Не найден модуль 'requests'. Установите зависимости: pip install -r requirements.txt")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(SCRIPT_DIR, ".github_token")

GITHUB_OWNER = "dimaskoweb-cmd"
GITHUB_REPO = "eml_lab"
GITHUB_BRANCH = "main"

# Файлы, которые публикуются (путь в репозитории == путь локально, от корня папки)
FILES_TO_PUBLISH = ["index.html", "library.json"]

API_ROOT = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"


def load_token():
    if not os.path.isfile(TOKEN_PATH):
        print(f"Не найден файл с токеном: {TOKEN_PATH}")
        print("Создай его и положи туда GitHub Personal Access Token (см. инструкцию в начале файла).")
        sys.exit(1)
    with open(TOKEN_PATH, "r", encoding="utf-8") as f:
        token = f.read().strip()
    if not token:
        print(f"Файл {TOKEN_PATH} пустой.")
        sys.exit(1)
    return token


def api_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def check_response(resp, action):
    if resp.status_code >= 300:
        print(f"Ошибка на шаге «{action}»: HTTP {resp.status_code}")
        print(resp.text[:2000])
        sys.exit(1)
    return resp.json()


def main():
    token = load_token()
    headers = api_headers(token)

    missing = [name for name in FILES_TO_PUBLISH if not os.path.isfile(os.path.join(SCRIPT_DIR, name))]
    if missing:
        print(f"Не найдены файлы для публикации: {', '.join(missing)}")
        print("Сначала запусти run_parser.bat, чтобы их сгенерировать.")
        sys.exit(1)

    print(f"Публикация в {GITHUB_OWNER}/{GITHUB_REPO} (ветка {GITHUB_BRANCH})...")

    # 1. Текущий коммит ветки
    ref = check_response(
        requests.get(f"{API_ROOT}/git/ref/heads/{GITHUB_BRANCH}", headers=headers),
        "получение ref ветки",
    )
    base_commit_sha = ref["object"]["sha"]

    base_commit = check_response(
        requests.get(f"{API_ROOT}/git/commits/{base_commit_sha}", headers=headers),
        "получение базового коммита",
    )
    base_tree_sha = base_commit["tree"]["sha"]

    # 2. Blob на каждый файл
    tree_entries = []
    for name in FILES_TO_PUBLISH:
        path = os.path.join(SCRIPT_DIR, name)
        with open(path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode("ascii")
        blob = check_response(
            requests.post(
                f"{API_ROOT}/git/blobs",
                headers=headers,
                json={"content": content_b64, "encoding": "base64"},
            ),
            f"загрузка blob для {name}",
        )
        tree_entries.append({
            "path": name,
            "mode": "100644",
            "type": "blob",
            "sha": blob["sha"],
        })
        print(f"  ✓ {name} загружен как blob {blob['sha'][:10]}")

    # 3. Новое дерево поверх текущего
    new_tree = check_response(
        requests.post(
            f"{API_ROOT}/git/trees",
            headers=headers,
            json={"base_tree": base_tree_sha, "tree": tree_entries},
        ),
        "создание дерева",
    )

    # 4. Новый коммит
    commit_message = "update: обновление дашборда (index.html + library.json)"
    new_commit = check_response(
        requests.post(
            f"{API_ROOT}/git/commits",
            headers=headers,
            json={
                "message": commit_message,
                "tree": new_tree["sha"],
                "parents": [base_commit_sha],
            },
        ),
        "создание коммита",
    )

    # 5. Перевод ветки на новый коммит
    check_response(
        requests.patch(
            f"{API_ROOT}/git/refs/heads/{GITHUB_BRANCH}",
            headers=headers,
            json={"sha": new_commit["sha"]},
        ),
        "обновление ветки",
    )

    print(f"Готово: коммит {new_commit['sha'][:10]} опубликован в {GITHUB_BRANCH}.")
    print(f"Дашборд обновится на https://{GITHUB_OWNER}.github.io/{GITHUB_REPO}/ через пару минут.")


if __name__ == "__main__":
    main()
