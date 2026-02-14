# ValgACE-multi

[![Klipper](https://img.shields.io/badge/compatible-Klipper-blue?logo=klipper&logoColor=white)](https://github.com/Klipper3d/klipper)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9+-blue?logo=python&logoColor=white)](https://www.python.org/)

**ValgACE-multi** — это расширенный модуль для Klipper, обеспечивающий управление массивом устройств автоматической смены филамента (ACE) от Anycubic. Поддерживает до **4 устройств** (16 слотов филамента) с возможностью сушки, подачи и возврата филамента.

---

## 📋 Содержание

- [Основные возможности](#-основные-возможности)
- [Требования](#-требования)
- [Установка](#-установка)
- [Конфигурация](#-конфигурация)
- [Использование](#-использование)
- [G-code команды](#g-code-команды)
- [G-code макросы](#g-code-макросы)
- [Протокол связи](#протокол-связи)
- [Архитектура](#архитектура)
- [Миграция с single-device](#миграция-с-single-device)
- [Решение проблем](#-решение-проблем)
- [Вклад в проект](#-вклад-в-проект)
- [Лицензия](#-лицензия)
- [Благодарности](#-благодарности)

---

## 🚀 Основные возможности

| Функция | Описание |
|---------|----------|
| **Multi-device support** | Управление до 4 устройствами ACE (16 слотов) |
| **Автоматическая смена инструмента** | Полная поддержка G-code команд `T0`-`T15` |
| **Сушка филамента** | Управление сушилкой на каждом устройстве |
| **Вспомогательная подача** | Feed assist для надежной подачи филамента |
| **Aggressive parking** | Альтернативный алгоритм парковки с датчиком филамента |
| **Infinity Spool** | Бесконечная смена катушек по заданному порядку |
| **Moonraker API** | Интеграция с Moonraker для статуса через UI |
| **Обратная совместимость** | Работа с существующими конфигурациями |

---

## 📋 Требования

### Системные требования

| Компонент | Минимальная версия | Примечание |
|-----------|-------------------|------------|
| **Klipper** | v0.11.0+ | Основная прошивка принтера |
| **Python** | 3.9+ | Для модуля ValgAce |
| **pyserial** | 3.5+ | Библиотека для serial-соединения |
| **Moonraker** | v0.8.0+ | Для интеграции с UI (опционально) |

### Зависимости Python

```
pyserial>=3.5
```

Установка зависимостей:
```bash
pip install pyserial
```

### Железо

- **Устройства ACE Pro** (Anycubic Color Engine Pro)
- **USB-C кабели** для подключения устройств к принтеру
- **Датчик филамента** (опционально, для aggressive parking)

---

## 📦 Установ��а

### Шаг 1: Скопируйте модуль

Скопируйте файл [`extras/ace.py`](extras/ace.py) в папку конфигурации Klipper:

```bash
cp extras/ace.py ~/printer_data/config/
```

### Шаг 2: Обновите конфигурацию

Добавьте конфигурацию в ваш `printer.cfg` или отдельный файл `ace.cfg`:

```ini
[save_variables]
filename: ~/printer_data/config/vars.cfg

[ace]
# Максимальное количество устройств (1-4)
max_devices: 4

# Serial порты для каждого устройства
serial: /dev/ttyACM0
serial_1: /dev/ttyACM1
serial_2: /dev/ttyACM2
serial_3: /dev/ttyACM3

# Общие параметры
baud: 115200
feed_speed: 25
retract_speed: 25
```

### Шаг 3: Перезапустите Klipper

```bash
# В веб-интерфейсе (Fluidd/Mainsail)
# Меню → System → Restart Klipper

# Или через SSH
moonraker_api.sh machine.restart
```

---

## ⚙️ Конфигурация

### Базовая конфигурация

```ini
[ace]
# === Multi-Device Configuration ===
max_devices: 4

# Serial ports (device 0 uses 'serial', others use 'serial_N')
serial: /dev/ttyACM0
serial_1: /dev/ttyACM1
serial_2: /dev/ttyACM2
serial_3: /dev/ttyACM3

baud: 115200

# === Feeding Parameters ===
feed_speed: 25
retract_speed: 25
retract_mode: 0
toolchange_retract_length: 100

# === Parking Settings ===
park_hit_count: 5
aggressive_parking: False
max_parking_distance: 100
parking_speed: 10

# === Dryer Settings ===
max_dryer_temperature: 55

# === Advanced Settings ===
disable_assist_after_toolchange: False
infinity_spool_mode: False
#filament_sensor: FilamentSensor
```

### Параметры конфигурации

| Параметр | По умолчанию | Описание |
|----------|--------------|----------|
| `max_devices` | 4 | Максимальное количество устройств (1-4) |
| `serial` | `/dev/ttyACM0` | Serial порт устройства 0 (обратная совместимость) |
| `serial_1` | None | Serial порт устройства 1 |
| `serial_2` | None | Serial порт устройства 2 |
| `serial_3` | None | Serial порт устройства 3 |
| `baud` | 115200 | Скорость соединения (бит/с) |
| `feed_speed` | 25 | Скорость подачи (мм/с) |
| `retract_speed` | 25 | Скорость втягивания (мм/с) |
| `retract_mode` | 0 | Режим втягивания (0=нормальный, 1=усиленный) |
| `toolchange_retract_length` | 100 | Длина втягивания при смене инструмента (мм) |
| `park_hit_count` | 5 | Количество проверок для завершения парковки |
| `aggressive_parking` | False | Использовать агрессивную парковку с датчиком |
| `max_parking_distance` | 100 | Максимальное расстояние парковки (мм) |
| `parking_speed` | 10 | Скорость парковки (мм/с) |
| `max_dryer_temperature` | 55 | Максимальная температура сушилки (°C) |
| `disable_assist_after_toolchange` | False | Отключать feed assist после смены инструмента |
| `infinity_spool_mode` | False | Включить режим бесконечной катушки |
| `filament_sensor` | None | Имя датчика филамента (для aggressive parking) |

---

## 🎮 Использование

### Схема адресации слотов

| Глобальный слот | Device ID | Local Slot | G-code |
|-----------------|-----------|------------|--------|
| 0 | 0 | 0 | T0 |
| 1 | 0 | 1 | T1 |
| 2 | 0 | 2 | T2 |
| 3 | 0 | 3 | T3 |
| 4 | 1 | 0 | T4 |
| 5 | 1 | 1 | T5 |
| 6 | 1 | 2 | T6 |
| 7 | 1 | 3 | T7 |
| 8 | 2 | 0 | T8 |
| 9 | 2 | 1 | T9 |
| 10 | 2 | 2 | T10 |
| 11 | 2 | 3 | T11 |
| 12 | 3 | 0 | T12 |
| 13 | 3 | 1 | T13 |
| 14 | 3 | 2 | T14 |
| 15 | 3 | 3 | T15 |

**Формула:** `global_slot = device_id * 4 + local_slot`

### Примеры использования

#### Смена инструмента

```gcode
; Смена на слот 0 (устройство 0, слот 0)
T0

; Смена на слот 5 (устройство 1, слот 1)
T5

; Смена на слот 12 (устройство 3, слот 0)
T12

; Выгрузка текущего инструмента
TR
```

#### Запрос статуса

```gcode
; Статус всех устройств
ACE_STATUS

; Статус конкретного устройства
ACE_DEVICE_STATUS DEVICE=1
```

#### Сушка филамента

```gcode
; Сушка на устройстве 0
START_DRYING DEVICE=0 TEMP=50 TIME=120

; Сушка на устройстве 2
START_DRYING DEVICE=2 TEMP=55 TIME=240

; Остановка сушки на устройстве 0
STOP_DRYING DEVICE=0
```

#### Подача/втягивание

```gcode
; Подача 200мм в слот 0
FEED_ACE INDEX=0 LENGTH=200 SPEED=25

; Втягивание 100мм из слот 5
RETRACT_ACE INDEX=5 LENGTH=100 SPEED=25

; Вспомогательная подача для слота 3
ENABLE_FEED_ASSIST INDEX=3
DISABLE_FEED_ASSIST INDEX=3
```

---

## 📜 G-code команды

### Основные команды

| Команда | Параметры | Описание |
|---------|-----------|----------|
| `ACE_STATUS` | - | Получить статус всех устройств |
| `ACE_LIST_DEVICES` | - | Список всех настроенных устройств |
| `ACE_DEVICE_STATUS` | `DEVICE=<id>` | Статус конкретного устройства |
| `ACE_CHANGE_TOOL` | `TOOL=<index>` | Смена инструмента (0-15, -1 для выгрузки) |
| `ACE_INFINITY_SPOOL` | - | Смена инструмента в режиме бесконечной катушки |
| `ACE_SET_INFINITY_SPOOL_ORDER` | `ORDER="0,1,2,3"` | Установить порядок слотов для бесконечной катушки |

### Управление подачей

| Команда | Параметры | Описание |
|---------|-----------|----------|
| `ACE_FEED` | `INDEX=<n> LENGTH=<mm> [SPEED=<mm/s>]` | Подача филамента |
| `ACE_RETRACT` | `INDEX=<n> LENGTH=<mm> [SPEED=<mm/s>] [MODE=<0|1>]` | Втягивание филамента |
| `ACE_PARK_TO_TOOLHEAD` | `INDEX=<n>` | Парковка филамента к соплу |
| `ACE_ENABLE_FEED_ASSIST` | `INDEX=<n>` | Включить вспомогательную подачу |
| `ACE_DISABLE_FEED_ASSIST` | `INDEX=<n>` | Выключить вспомогательную подачу |

### Управление сушилкой

| Команда | Параметры | Описание |
|---------|-----------|----------|
| `ACE_START_DRYING` | `TEMP=<°C> DURATION=<мин> [DEVICE=<id>]` | Запустить сушку |
| `ACE_STOP_DRYING` | `[DEVICE=<id>]` | Остановить сушку |

### Управление подключением

| Команда | Параметры | Описание |
|---------|-----------|----------|
| `ACE_CONNECT` | `[DEVICE=<id>]` | Подключиться к устройству |
| `ACE_DISCONNECT` | `[DEVICE=<id>]` | Отключиться от устройства |
| `ACE_CONNECTION_STATUS` | `[DEVICE=<id>]` | Статус подключения |
| `ACE_DEBUG` | `METHOD=<name> [PARAMS=<json>] [DEVICE=<id>]` | Отладочная команда |

### Информация о филаменте

| Команда | Параметры | Описание |
|---------|-----------|----------|
| `ACE_FILAMENT_INFO` | `INDEX=<n>` | Получить информацию о филаменте в слоте |
| `ACE_CHECK_FILAMENT_SENSOR` | - | Проверить статус датчика филамента |

---

## 🧩 G-code макросы

### Базовые макросы

```ini
[gcode_macro T0]
gcode:
    ACE_CHANGE_TOOL TOOL=0

[gcode_macro T1]
gcode:
    ACE_CHANGE_TOOL TOOL=1

; ... и так далее до T15

[gcode_macro TR]
gcode:
    ACE_CHANGE_TOOL TOOL=-1
```

### Макросы подачи/втягивания

```ini
[gcode_macro FEED_ACE]
gcode:
    {% if params.INDEX and params.LENGTH is defined %}
        {% set target_index = params.INDEX|int %}
        {% set target_length = params.LENGTH|int %}
        {% if params.SPEED is defined %} 
            {% set target_speed = params.SPEED|int %}
        {% else %}
            {% set target_speed = 25 %}
        {% endif %}
        M118 Включена подача филамента слот {target_index}.
        ACE_FEED INDEX={target_index} LENGTH={target_length} SPEED={target_speed}
    {% else %}
        {action_respond_info("Error: INDEX or LENGTH is missing")}
    {% endif %}

[gcode_macro RETRACT_ACE]
gcode:
    {% if params.INDEX and params.LENGTH is defined %}
        {% set target_index = params.INDEX|int %}
        {% set target_length = params.LENGTH|int %}
        {% if params.SPEED is defined %} 
            {% set target_speed = params.SPEED|int %}
        {% else %}
            {% set target_speed = 25 %}
        {% endif %}
        M118 Включено втягивание филамента слот {target_index}.
        ACE_RETRACT INDEX={target_index} LENGTH={target_length} SPEED={target_speed}
    {% else %}
        {action_respond_info("Error: INDEX or LENGTH is missing")}
    {% endif %}
```

### Макросы сушки

```ini
[gcode_macro START_DRYING]
gcode:
    {% set device_id = params.DEVICE|default(0)|int %}
    {% set target_temp = params.TEMP|default(55)|int %}
    {% set target_time = params.TIME|default(120)|int %}
    
    M118 Запущена сушка устройства {device_id}: {target_temp}°C, {target_time} мин.
    ACE_START_DRYING DEVICE={device_id} TEMP={target_temp} DURATION={target_time}

[gcode_macro STOP_DRYING]
gcode:
    {% set device_id = params.DEVICE|default(0)|int %}
    ACE_STOP_DRYING DEVICE={device_id}
    M118 Сушка устройства {device_id} остановлена.
```

### Хуки смены инструмента

```ini
[gcode_macro _ACE_PRE_TOOLCHANGE]
gcode:
    M118 Подготовка к смене филамента

[gcode_macro _ACE_POST_TOOLCHANGE]
gcode:
    M118 Действия после смены филамента

[gcode_macro _ACE_PRE_INFINITYSPOOL]
gcode:
    M118 Подготовка к смене филамента (Infinity Spool)

[gcode_macro _ACE_POST_INFINITYSPOOL]
gcode:
    M118 Действия после смены филамента (Infinity Spool)

[gcode_macro _ACE_ON_EMPTY_ERROR]
gcode:
    {action_respond_info("Spool is empty")}
    {% if printer.idle_timeout.state == "Printing" %}
        PAUSE
    {% endif %}
```

---

## 📡 Протокол связи

### Формат фрейма

```
┌─────────────┬──────────────┬──────────┬───────┬─────────┐
│ 0xFF 0xAA   │ Length (2B)  │ Payload  │ CRC   │ 0xFE    │
│ (Header)    │ (LE)         │ (JSON)   │ (2B)  │ (End)   │
└─────────────┴──────────────┴──────────┴───────┴─────────┘
```

### JSON-RPC запрос

```json
{
  "id": 1,
  "method": "feed_filament",
  "params": {
    "index": 0,
    "length": 200,
    "speed": 25
  }
}
```

### JSON-RPC ответ

```json
{
  "id": 1,
  "result": {},
  "code": 0,
  "msg": "success"
}
```

### Поддержанные методы

| Метод | Параметры | Описание |
|-------|-----------|----------|
| `get_info` | - | Получить информацию об устройстве |
| `get_status` | - | Получить статус устройства и слотов |
| `get_filament_info` | `index` | Получить информацию о филаменте |
| `feed_filament` | `index`, `length`, `speed` | Подать филамент |
| `stop_feed_filament` | `index` | Остановить подачу |
| `update_feeding_speed` | `index`, `speed` | Обновить скорость подачи |
| `unwind_filament` | `index`, `length`, `speed`, `mode` | Втянуть филамент |
| `stop_unwind_filament` | `index` | Остановить втягивание |
| `update_unwinding_speed` | `index`, `speed` | Обновить скорость втягивания |
| `start_feed_assist` | `index` | Включить вспомогательную подачу |
| `stop_feed_assist` | `index` | Выключить вспомогательную подачу |
| `drying` | `temp`, `fan_speed`, `duration` | Запустить сушку |
| `drying_stop` | - | Остановить сушку |

---

## 🏗️ Архитектура

### Классы модуля

```
ValgAce (Manager)
├── Управляет массивом устройств
├── Координирует G-code команды
└── Предоставляет единый API для Klipper

ACEDevice (Device)
├── Инкапсулирует одно физическое устройство
├── Управляет serial-соединением
├── Содержит состояние 4 слотов
└── Обрабатывает таймеры чтения/записи
```

### Схема взаимодействия

```
┌─────────────────────────────────────────────────────────────┐
│                    Klipper Core                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  G-code Parser & Executor                             │  │
│  │  - T0-T15 commands                                    │  │
│  │  - ACE_* commands                                     │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ G-code commands
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    ValgAce (Manager)                        │
│  ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────┐ │
│  │  Device 0   │ │  Device 1   │ │  Device 2   │ │ ...     │ │
│  │  /dev/ACE1  │ │  /dev/ACE2  │ │  /dev/ACE3  │ │         │ │
│  └──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └─────────┘ │
│         │                │                │                   │
│         ▼                ▼                ▼                   │
│  ┌────────────────────────────────────────────────────────┐  │
│  │  ACEDevice (per-device class)                          │  │
│  │  - Serial connection                                   │  │
│  │  - Request queue                                       │  │
│  │  - Response handling                                   │  │
│  │  - Parking state management                            │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ USB/Serial
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              ACE Devices (Anycubic Color Engine Pro)        │
└─────────────────────────────────────────────────────────────┘
```

### Маппинг слотов

```python
# Глобальный слот → (device_id, local_slot)
device_id = global_slot // 4
local_slot = global_slot % 4

# Обратно
global_slot = device_id * 4 + local_slot
```

---

## 🔄 Миграция с single-device

### Шаг 1: Обновите конфигурацию

**Старая конфигурация:**
```ini
[ace]
serial: /dev/ttyACM0
baud: 115200
```

**Новая конфигурация (обратная совместимость):**
```ini
[ace]
max_devices: 4
serial: /dev/ttyACM0
baud: 115200
```

### Шаг 2: Добавьте дополнительные устройства (опционально)

```ini
[ace]
max_devices: 2
serial: /dev/ttyACM0
serial_1: /dev/ttyACM1
baud: 115200
```

### Шаг 3: Обновите макросы (если используются)

Макросы `T0`-`T3` работают без изменений. Для новых слотов добавьте:

```ini
[gcode_macro T4]
gcode:
    ACE_CHANGE_TOOL TOOL=4

[gcode_macro T5]
gcode:
    ACE_CHANGE_TOOL TOOL=5
```

### Шаг 4: Проверьте статус

```gcode
ACE_LIST_DEVICES
ACE_STATUS
```

---

## 🔧 Решение проблем

### Устройство не подключается

**Проверьте:**
1. Правильность serial-порта (`ls /dev/ttyACM*`)
2. Права доступа (`sudo usermod -a -G dialout $USER`)
3. Скорость соединения (должна быть 115200)
4. Физическое подключение USB

**Отладка:**
```gcode
ACE_DEBUG METHOD=get_info
ACE_CONNECTION_STATUS
```

### Ошибка парковки

**Причины:**
1. Недостаточно `park_hit_count` для вашей настройки
2. Проблемы с feed assist
3. Филамент застрял

**Решение:**
```ini
[ace]
park_hit_count: 3  # Уменьшить для более чувствительной проверки
aggressive_parking: True  # Ис��ользовать датчик филамента
```

### Статус слота "empty" вместо "ready"

**Проверьте:**
1. RFID-тег на катушке
2. Правильную установку катушки в слот
3. Статус RFID: `ACE_FILAMENT_INFO INDEX=<n>`

### Таймауты операций

**Увеличьте таймауты в коде:**
```python
# В ace.py, класс ACEDevice
self._response_timeout = config.getfloat('response_timeout', 5.0)
self._read_timeout = config.getfloat('read_timeout', 0.2)
```

---

## 🤝 Вклад в проект

Мы приветствуем вклад в проект! Вот как вы можете помочь:

### Как внести правки

1. **Форкните репозиторий**
2. **Создайте ветку для вашей функции**
   ```bash
   git checkout -b feature/AmazingFeature
   ```
3. **Сделайте коммит**
   ```bash
   git commit -m 'Add some AmazingFeature'
   ```
4. **Отправьте в ветку**
   ```bash
   git push origin feature/AmazingFeature
   ```
5. **Откройте Pull Request**

### Правила коммитов

Мы используем [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <description>

[optional body]

<type> = feat | fix | docs | style | refactor | test | chore
```

**Примеры:**
- `feat: add support for 4 devices`
- `fix: correct parking timeout calculation`
- `docs: update README with installation guide`
- `refactor: extract ACEDevice class`

### Кодекс поведения

Мы придерживаемся [Contributor Covenant](https://www.contributor-covenant.org/):

- Уважайте других участников
- Конструктивная критика приветствуется
- Помогайте новичкам
- Будьте терпеливы и доброжелательны

---

## 📄 Лицензия

Этот проект распространяется под лицензией [MIT](LICENSE).

```
MIT License

Copyright (c) 2026 ValgACE-multi Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## 🙏 Благодарности

- **[Klipper3d](https://github.com/Klipper3d/klipper)** — за отличную прошивку для 3D-принтеров
- **[Anycubic](https://www.anycubic.com/)** — за ACE Pro устройство
- **[Moonraker](https://github.com/Arksine/moonraker)** — за API для интеграции с UI
- **Всем контрибьюторам** — за тестирование и обратную связь

---

## 📞 Поддержка

### Документация

- [Protocol.md](docs/Protocol.md) — детальное описание протокола связи
- [MULTI_DEVICE_PLAN.md](docs/MULTI_DEVICE_PLAN.md) — план масштабирования

### Контакты

- **GitHub Issues**: [https://github.com/valgace/valgace-multi/issues](https://github.com/valgace/valgace-multi/issues)
- **Discord**: [Klipper Discord Server](https://discord.gg/k3bBn9t) (канал #3d-printers)

---

## 📊 Статус проекта

| Компонент | Статус |
|-----------|--------|
| Multi-device support | ✅ Готово |
| G-code commands | ✅ Готово |
| Moonraker API | ✅ Готово |
| Infinity Spool | ✅ Готово |
| Aggressive parking | ✅ Готово |
| Документация | ✅ Готово |

---

*Документация создана: 2026-02-14*  
*Версия модуля: 2.0.0*
