# Router Configurator

HTTP-сервис обновления списков доменов/IP и DNS-маршрутов на уже настроенных
роутерах Netcraze/Keenetic через RCI API. Адрес каждого роутера приходит в поле
`hostname` POST-запроса. Списка роутеров и регистрации устройств нет.

Генерация листа выполняется отдельной CLI-командой. HTTP-сервис читает готовый
TXT, сохраняет его снимок в SQLite и ставит обновление в очередь. Задачи для разных
роутеров выполняются параллельно; задачи для одного роутера — последовательно.
Проект самостоятелен: каталог `ip-addresses` для работы не нужен.

**`update` заменяет все существующие списки и правила DNS-маршрутизации.**
Во время выполнения отключаются найденные WireGuard-интерфейсы. В конце включается
первый из них; остальные остаются выключенными, как в исходном алгоритме.
Автоматический откат конфигурации и повтор прерванной задачи не выполняются.

## Установка и конфигурация

Требования: Python 3.10+, сетевой доступ к RCI API роутеров. Для скачивания баз
генератору нужен доступ к GitHub. Для постоянного запуска доступны Linux/systemd
и Docker Compose с Linux-контейнерами.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

На Windows для разработки:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Заполните `.env`:

```dotenv
API_KEY=replace-with-a-random-secret
ROUTER_USERNAME=admin
ROUTER_PASSWORD='your-router-password'
LIST_PATH=lists/router.txt
DATA_DIR=.data
HOST=127.0.0.1
PORT=8765
MAX_PARALLEL_JOBS=4
```

`API_KEY`, `ROUTER_USERNAME` и `ROUTER_PASSWORD` обязательны. Логин и пароль общие
для всех роутеров. Путь `LIST_PATH` указывает на **лист доменов/IP**, а не список
устройств. `DATA_DIR` содержит SQLite, историю задач и блокировку обработчика.
`MAX_PARALLEL_JOBS` задаёт число одновременно выполняемых задач (1–32, по умолчанию
4). Значение `1` включает последовательное выполнение всей очереди.

Случайный ключ можно получить командой `python3 -c "import secrets;
print(secrets.token_urlsafe(32))"`. Храните `.env` с доступом только для владельца
и пользователя службы. Не добавляйте его в Git.

Все относительные пути конфигурации разрешаются от каталога `.env`, независимо
от текущего рабочего каталога. Можно выбрать другой файл через `--env-file`.
Переменные окружения имеют приоритет над `.env`. Подстановка `${...}` в значениях
отключена, чтобы пароль читался буквально. Конфигурация загружается при запуске;
для изменения ключа или учётных данных перезапустите службу.

## Создание листа

Пример пресета находится в `preset.example.txt`:

```text
mode: proxy
geosite:openai
geosite:youtube
geoip:telegram
domain:example.com
domain:203.0.113.10/32
```

`proxy` направляет выбранные адреса через первый WireGuard-интерфейс и поднимает
приоритет Ethernet ISP. `direct` направляет выбранные адреса через Ethernet ISP
и помещает его в конец списка приоритетов.

```bash
.venv/bin/python generate_geo_domains.py \
  --preset preset.example.txt --output lists/router.txt --cache-dir .cache
```

Сервис не запускает генерацию самостоятельно. CLI скачивает только необходимые
для пресета базы `geosite.dat` и `geoip.dat` из v2ray-rules-dat. При каждом онлайн
запуске скачиваются свежие базы; скачанные базы остаются в `--cache-dir`.

Повторная генерация без доступа к сети:

```bash
.venv/bin/python generate_geo_domains.py \
  --preset preset.example.txt --output lists/router.txt --cache-dir .cache --offline
```

Относительные пути CLI считаются от текущего каталога. Пресет, содержащий только
`domain:`, не требует баз или доступа к сети. IPv6 по умолчанию исключается.
Из geosite экспортируются доменные записи типов RootDomain и Full. Regex и keyword
пропускаются с сообщением CLI: TXT-лист хранит домены/IP, а не правила сопоставления
V2Ray. Например, одна regex-запись в категории `openai` не переносится в лист.
Неизвестная категория, некорректный пресет, ошибка загрузки или пустой результат
завершают CLI с кодом `1` и сохраняют предыдущий выходной лист.

Формат результата — UTF-8, без префиксов `domain:`, `geosite:` или `geoip:`:

```text
mode: proxy
example.com
203.0.113.0/24
```

Разрешены комментарии `#` и пустые строки. Домены нормализуются, IP/CIDR
проверяются, дубликаты удаляются с сохранением порядка. Лист публикуется атомарной
заменой файла. При загрузке на роутер домены и IP разделяются на группы до
299 записей с именами `geosite_domains_N` и `geoip_ips_N`.

## Запуск и HTTP API

```bash
.venv/bin/python -m router_configurator --env-file .env
```

На Windows:

```powershell
.\.venv\Scripts\python.exe -m router_configurator --env-file .env
```

По умолчанию сервис слушает `127.0.0.1:8765`. Для внешнего доступа задайте `HOST`
и разместите сервис за HTTPS reverse proxy. Запускайте один процесс: очередь
обрабатывается пулом потоков, размер которого задан в `MAX_PARALLEL_JOBS`.
SQLite атомарно выбирает старейшую задачу для свободного роутера; ожидающая задача
для занятого роутера не задерживает задачи для остальных устройств. Блокировка
каталога данных предотвращает запуск второго процесса с той же SQLite.

Роутер определяется по нормализованному имени хоста: регистр, завершающая точка,
схема HTTP/HTTPS и порт не позволяют запустить обновления одного hostname
одновременно. Разные DNS-имена и IP считаются разными адресами; для одного
устройства используйте одинаковый hostname. Старая SQLite обновляется при запуске
автоматически, с сохранением очереди, снимков и истории.

Перед примерами задайте `ROUTER_API_KEY` в окружении вызывающего клиента.
Авторизация всех маршрутов API — заголовок `X-API-Key`.

```bash
curl -sS -X POST http://127.0.0.1:8765/api/v1/router-configurations \
  -H "X-API-Key: $ROUTER_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"hostname":"192.168.1.1","action":"update"}'
```

Ответ `202 Accepted`:

```json
{
  "job_id": "e1b93917-fb6c-43b3-923b-e11b81ff119a",
  "status": "queued",
  "status_url": "/api/v1/jobs/e1b93917-fb6c-43b3-923b-e11b81ff119a"
}
```

До ответа `202` лист уже проверен, а его снимок и задача записаны одной
транзакцией SQLite. Новая генерация TXT влияет только на последующие POST.
Повторный POST создаёт отдельную задачу; идемпотентность запросов не реализована.

Получение результата:

```bash
curl -sS http://127.0.0.1:8765/api/v1/jobs/e1b93917-fb6c-43b3-923b-e11b81ff119a \
  -H "X-API-Key: $ROUTER_API_KEY"
```

Ответ содержит `job_id`, `hostname`, `action`, `status`, `created_at`, `started_at`,
`finished_at`, `message`, `error` и `events` (время и текст этапов выполнения).
Время — ISO 8601 в UTC. Не наступившие даты и ошибка успешной задачи равны `null`.
Статусы: `queued`, `running`, `succeeded`, `failed`. Результаты доступны после
перезапуска. Секреты и полный снимок листа в ответ не включаются.

| Код | Причина |
| --- | --- |
| 202 | Задача принята в очередь |
| 200 | Статус задачи получен |
| 401 | Отсутствует или неверен API-ключ |
| 404 | Неизвестный идентификатор задачи |
| 422 | Неверное тело запроса, hostname, action или дополнительные поля |
| 503 | Настроенный лист недоступен, пуст или некорректен |

`hostname` допускает IPv4/IPv6, доменное имя и базовый HTTP(S) URL с портом.
Для IP без схемы используется HTTP, для домена — HTTPS. Например:
`192.168.1.1`, `192.168.1.1:8080`, `router.example.com`,
`https://router.example.com:8443`, `[2001:db8::1]:8080`.
URL с учётными данными, путём, query или fragment отклоняется. Перенаправления
HTTP при обращении к роутеру не выполняются.

Выполнение может занимать несколько минут. Отсутствие WireGuard, а для режима
`direct` — WAN ISP, обнаруживается до изменений конфигурации. Ошибки авторизации,
команд RCI и связи сохраняются в задаче. Созданные списки и маршруты перечитываются;
недостающие создаются повторно в пределах трёх попыток.

## Развёртывание в Docker

Нужны Docker Engine и Docker Compose v2 либо Docker Desktop с Linux-контейнерами.
Python на хосте не требуется. Команды выполняются из каталога проекта.

Если `.env` уже заполнен, используйте его. На новой машине создайте `.env` из
`.env.example` и задайте `API_KEY`, `ROUTER_USERNAME`, `ROUTER_PASSWORD` и
`MAX_PARALLEL_JOBS`. Создайте каталоги `lists` и `.data`:

```bash
# Только при первой настройке, если .env ещё нет:
cp .env.example .env
mkdir -p lists .data
```

На Windows для первой настройки используйте `Copy-Item .env.example .env` и
`New-Item -ItemType Directory -Force lists, .data` в PowerShell. При существующем `.env`
копировать шаблон повторно не нужно.

Образ сервиса работает от UID/GID `10001:10001`. На Linux дайте этой группе доступ
на чтение конфигурации и запись в каталог данных:

```bash
sudo chgrp 10001 .env
chmod 640 .env
sudo chown -R 10001:10001 .data
sudo chmod 700 .data
```

Каталог `lists` должен быть доступен контейнеру для чтения и обхода. Обычные права
`0755` на каталог и `0644` на TXT подходят. На Windows доступ к подключённым
каталогам предоставляет Docker Desktop.

Соберите образ и сгенерируйте лист отдельной CLI-командой:

```bash
docker compose build
docker compose run --rm generator
```

Если `lists/router.txt` уже создан, генерацию можно пропустить. Сервис `generator`
включён в профиль `tools`: он запускается только явно и не работает постоянно.
Его файловые подключения — пресет только для чтения, `lists` для результата и
`.cache` для баз. Он выполняется от root для записи в каталоги хоста; готовый
TXT получает права `0644`. Логин, пароль и API-ключ генератору не передаются.

Для собственного пресета задайте `PRESET_PATH` в `.env`, например
`PRESET_PATH=./my-preset.txt`. Генерация из кеша без скачивания:

```bash
docker compose run --rm generator python generate_geo_domains.py --preset /config/preset.txt --output /output/router.txt --cache-dir /cache --offline
```

Запуск сервиса и просмотр состояния:

```bash
docker compose config --quiet
docker compose up -d router-configurator
docker compose ps
docker compose logs -f router-configurator
```

Журнал выводится в stdout и доступен через `docker compose logs` (или
`sudo docker compose logs`, если Docker требует sudo). Он содержит HTTP-метод,
шаблон маршрута, адрес клиента, код ответа и время обработки, а также принятие,
начало, этапы и результат задач. Для каждой задачи указаны `job_id`, `hostname`
и статус: так можно различать параллельные обновления. Подключение к RCI,
авторизация, переподключения, создание списков и DNS-маршрутов тоже видны в журнале.
API-ключ, учётные данные, заголовки, query-параметры, тела запросов и ответов
роутера не выводятся. История этапов также сохраняется в SQLite и доступна через
`GET /api/v1/jobs/{job_id}`.

После обновления кода журналирования пересоберите и пересоздайте сервис:

```bash
sudo docker compose up -d --build router-configurator
sudo docker compose logs -f router-configurator
```

API доступен на `http://127.0.0.1:8765`; формат запросов и `X-API-Key` те же.
`PORT` в `.env` задаёт порт публикации на хосте. Внутри контейнера сервис слушает
`0.0.0.0:8765`. Для публикации на другом адресе хоста задайте `DOCKER_BIND_HOST`,
например `DOCKER_BIND_HOST=0.0.0.0` для доступа из локальной сети.

Compose задаёт контейнерные пути `LIST_PATH=/app/lists/router.txt` и
`DATA_DIR=/data`; локальные значения этих параметров в `.env` используются при
обычном запуске Python. Готовый лист в Docker всегда берётся из `lists/router.txt`
на хосте. Подключён весь каталог, поэтому атомарная замена TXT при повторной
генерации видна сервису сразу и влияет только на новые POST.

Конфигурация подключается как файл только для чтения и не входит в образ. SQLite,
очередь и история находятся в каталоге `.data` рядом с `compose.yaml`, подключённом
в контейнер как `/data`. Пути `./.data` и `./lists` считаются от расположения
`compose.yaml`, в том числе при запуске Compose через `-f` из другого каталога.
Данные сохраняются при остановке и пересоздании контейнера и исключены из Git
и сборки образа. Для резервной копии остановите сервис и скопируйте `.data`.
Запускайте один экземпляр сервиса; локальный Python и Docker используют одну
базу и должны запускаться по очереди. Параллелизм внутри сервиса задаётся
`MAX_PARALLEL_JOBS`.

После изменения ключа, учётных данных или `MAX_PARALLEL_JOBS` в `.env`
перезапустите сервис. Для изменения порта или адреса публикации выполните
`docker compose up -d router-configurator`, чтобы пересоздать контейнер с новыми
параметрами Compose. После изменения кода пересоберите:

```bash
docker compose restart router-configurator
docker compose up -d --build router-configurator
```

Проверка healthcheck проверяет открытый HTTP-порт и не обращается к роутерам.
При остановке контейнер получает SIGTERM; Compose даёт до 900 секунд на завершение
активных задач. После аварии прерванные задачи отмечаются `failed`, а ожидающие
продолжаются, как при запуске через systemd.

```bash
docker compose down
```

Команда выше сохраняет каталог `.data` с задачами. Даже
`docker compose down --volumes` не удаляет подключённые каталоги хоста.
Для поддержки контейнеров и параметров Compose
см. [документацию Docker Compose](https://docs.docker.com/reference/compose-file/services/).

## Установка Linux/systemd

Разместите проект в `/opt/router-configurator`, создайте окружение и установите
зависимости командами выше. Затем выполните:

```bash
sudo useradd --system --home-dir /var/lib/router-configurator \
  --shell /usr/sbin/nologin router-configurator
sudo install -d -m 0750 -o root -g router-configurator /etc/router-configurator
sudo install -m 0640 -o root -g router-configurator .env.example \
  /etc/router-configurator/router-configurator.env
sudo install -d -m 0750 -o root -g router-configurator /opt/router-configurator/lists
sudoedit /etc/router-configurator/router-configurator.env
```

Укажите в файле конфигурации ключ и учётные данные, а также абсолютные пути:

```dotenv
LIST_PATH=/opt/router-configurator/lists/router.txt
DATA_DIR=/var/lib/router-configurator
```

Сгенерируйте лист с правами пользователя, имеющего доступ на запись в каталог
`lists`. Службе нужен доступ на чтение файла; генератор сохраняет права существующего
листа, новые файлы создаёт с правами `0644`.

```bash
sudo /opt/router-configurator/.venv/bin/python /opt/router-configurator/generate_geo_domains.py \
  --preset /opt/router-configurator/preset.example.txt \
  --output /opt/router-configurator/lists/router.txt \
  --cache-dir /opt/router-configurator/.cache
sudo install -m 0644 deploy/router-configurator.service \
  /etc/systemd/system/router-configurator.service
sudo systemd-analyze verify /etc/systemd/system/router-configurator.service
sudo systemctl daemon-reload
sudo systemctl enable --now router-configurator
sudo systemctl status router-configurator
sudo journalctl -u router-configurator -f
```

Unit запускает службу от отдельного пользователя, разрешает запись только в
`/var/lib/router-configurator` и перезапускает её при сбое. `StateDirectory`
создаёт каталог данных с нужным владельцем. У пользователя службы должна быть
возможность читать файлы проекта и выполнять Python из `.venv`.

При SIGTERM очередь прекращает брать новые задания и ожидает завершения всех
активных задач до 885 секунд суммарно. `TimeoutStopSec=900` ограничивает общую остановку. При аварии или
принудительном завершении оставшиеся `running` при следующем запуске получают
`failed` с причиной прерывания. Ожидающие задачи продолжаются с сохранённым листом.
Для повторного обновления отправьте новый POST. История автоматически не удаляется.

## Проверки

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Автоматические тесты блокируют реальные сетевые обращения. Проверяются HTTP API,
генерация онлайн/офлайн на искусственных protobuf-базах, атомарная публикация,
сохранение снимков и очереди, миграция существующей SQLite, параллельное выполнение
для разных роутеров, ограничение числа задач, сериализация одного hostname,
остановка и повторный запуск обработчиков. Имитация
RCI исполняет реальные формы команд, моделирует разрыв связи и неприменённый
маршрут. systemd проверяется отдельно на Linux.

Первичная установка WireGuard, импорт `.conf`, смена VPN endpoint, веб-панель,
автоматическая генерация по HTTP и автоматический откат не поддерживаются.
