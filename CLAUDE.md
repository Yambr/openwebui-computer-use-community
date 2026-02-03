# Инструкции для работы с проектом

## Сборка Docker-образа

Всегда собирать с `--platform linux/amd64` (сервер docker-ai x86_64):

```bash
docker build --platform linux/amd64 -t ai-computer-use-test:latest .
```

## Тестирование

После сборки образа и после любых правок в `Dockerfile`, `package.json`, `requirements.txt`, skills или npm-конфигурации — прогнать тесты:

```bash
./tests/test-docker-image.sh [image-name]
```

Дефолтный image: `ai-computer-use-test:latest`.

Тесты проверяют: доступность npm-пакетов (CommonJS `require()`, ESM `import`), CLI tools (mmdc, tsc, tsx, claude), Python packages, Playwright, html2pptx, размер volume (`/home/assistant/` < 1MB), права файлов.

## npm-пакеты: layout

Пакеты установлены вне `/home/assistant` (volume mount point), чтобы не дублироваться в каждом контейнере:

| Путь | Что | Где хранится |
|------|-----|--------------|
| `/home/node_modules/` | Библиотеки (react, pptxgenjs, pdf-lib...) | Image layer (shared) |
| `/usr/local/lib/node_modules_global/` | CLI tools (mmdc, tsc, tsx, claude) | Image layer (shared) |
| `/home/assistant/node_modules/` | Пользовательские пакеты (`npm install`) | Volume (per-container) |

Node.js использует parent directory resolution: если пакет не найден в `/home/assistant/node_modules`, он ищет в `/home/node_modules`.

## Структура проекта

- `Dockerfile` — образ контейнера
- `openwebui-tools/` — OpenWebUI Tools (bash, str_replace, file_create, view, sub_agent)
- `openwebui-functions/` — OpenWebUI Functions/Filters
- `skills/` — Skills для AI (pptx, xlsx, docx, pdf, gitlab-explorer, sub-agent)
- `docs/` — Документация
- `tests/` — Тесты
