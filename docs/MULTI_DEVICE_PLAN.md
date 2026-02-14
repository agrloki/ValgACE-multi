# План масштабирования ValgACE-multi: от 1 устройства к 4 устройствам

## Содержание
1. [Анализ текущей архитектуры](#1-анализ-текущей-архитектуры)
2. [Стратегия миграции](#2-стратегия-миграции)
3. [Изменения в ace.py](#3-изменения-в-acepy)
4. [Изменения в ace.cfg](#4-изменения-в-acecfg)
5. [Схема адресации 16 слотов](#5-схема-адресации-16-слотов)
6. [Обработка ошибок и таймаутов](#6-обработка-ошибок-и-таймаутов)
7. [Обратная совместимость](#7-обратная-совместимость)
8. [Этапы реализации](#8-этапы-реализации)

---

## 1. Анализ текущей архитектуры

### 1.1 Класс ValgAce (extras/ace.py)

**Структура класса:**

```
ValgAce
├── Атрибуты конфигурации
│   ├── serial_name: str          # Один serial порт
│   ├── baud: int                 # Скорость соединения
│   ├── feed_speed, retract_speed # Параметры подачи
│   └── ...                       # Другие параметры
├── Атрибуты состояния
│   ├── _serial: Serial           # Одно соединение
│   ├── _connected: bool          # Один флаг соединения
│   ├── _info: Dict               # Состояние одного устройства
│   └── _queue: Queue             # Одна очередь запросов
├── Атрибуты парковки
│   ├── _park_in_progress: bool
│   ├── _park_index: int          # Индекс 0-3
│   └── _feed_assist_index: int   # Индекс 0-3
└── Методы
    ├── _connect() / _disconnect()
    ├── send_request()
    ├── _reader_loop() / _writer_loop()
    └── cmd_ACE_*()               # G-code команды
```

### 1.2 Критические точки для миграции

| Компонент | Текущее состояние | Требуемое изменение |
|-----------|-------------------|---------------------|
| `serial_name` | Одна строка | Массив из 4 портов |
| `_serial` | Один объект Serial | Массив из 4 объектов |
| `_connected` | Один bool | Массив из 4 bool или Dict[int, bool] |
| `_info` | Dict с 4 слотами | Dict с 16 слотами или Dict[int, DeviceInfo] |
| `_queue` | Одна очередь | Одна общая или 4 отдельных очереди |
| `_callback_map` | Один map | Один общий с device_id в ключе |
| `_reader_timer` | Один таймер | 4 таймера или один с циклом по устройствам |
| `_park_index` | int 0-3 | Кортеж (device_id, slot_id) или int 0-15 |

### 1.3 Протокол (docs/Protocol.md)

**Ключевые особенности:**
- JSON-RPC через USB CDC (serial)
- Фрейминг: `0xFF 0xAA` + length + payload + CRC + `0xFE`
- Параметр `index` во всех методах = номер слота (0-3)
- Keepalive: каждые 3 секунды

**Методы протокола:**
- `get_info` - информация об устройстве (4 слота)
- `get_status` - статус устройства и слотов
- `get_filament_info` - информация о филаменте в слоте
- `feed_filament` / `stop_feed_filament` - подача
- `unwind_filament` / `stop_unwind_filament` - втягивание
- `start_feed_assist` / `stop_feed_assist` - вспомогательная подача
- `drying` / `drying_stop` - сушка

---

## 2. Стратегия миграции

### 2.1 Архитектурный паттерн: Device Manager

Предлагается выделить два класса:

```
┌─────────────────────────────────────────────────────────────┐
│                        ValgAce (Manager)                     │
│  - Управляет массивом устройств                              │
│  - Координирует G-code команды                               │
│  - Предоставляет единый API для Klipper                      │
└─────────────────────────────────────────────────────────────┘
                              │
                              │ управляет
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                    ACEDevice (Device)                        │
│  - Инкапсулирует одно физическое устройство                  │
│  - Управляет serial-соединением                              │
│  - Содержит состояние 4 слотов                               │
│  - Обрабатывает таймеры чтения/записи                        │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Преимущества подхода

1. **Инкапсуляция** - каждое устройство независимо
2. **Масштабируемость** - легко добавить 5-е, 6-е устройство
3. **Изоляция ошибок** - сбой одного устройства не влияет на другие
4. **Тестируемость** - можно тестировать одно устройство изолированно
5. **Обратная совместимость** - при одном устройстве поведение идентично текущему

### 2.3 Схема классов

```python
class ACEDevice:
    """Представляет одно физическое ACE устройство (4 слота)"""
    
    def __init__(self, device_id: int, config: ConfigWrapper):
        self.device_id: int
        self.serial_port: str
        self.serial: Serial
        self.connected: bool
        self.info: Dict[str, Any]  # Состояние устройства
        self.queue: Queue
        self.callback_map: Dict[int, Callable]
        # ... таймеры, буферы
        
    def connect(self) -> bool: ...
    def disconnect(self): ...
    def send_request(self, request: Dict, callback: Callable): ...
    def get_slot_status(self, slot: int) -> Dict: ...


class ValgAce:
    """Менеджер массива ACE устройств"""
    
    def __init__(self, config: ConfigWrapper):
        self.devices: Dict[int, ACEDevice]  # device_id -> ACEDevice
        self.active_device: int  # Текущее активное устройство
        self.global_slot_mapping: Dict[int, Tuple[int, int]]  # global_slot -> (device_id, local_slot)
        # ... общие параметры
        
    def get_device_for_slot(self, global_slot: int) -> ACEDevice: ...
    def get_local_slot(self, global_slot: int) -> int: ...
    def broadcast_command(self, method: str, params: Dict): ...
```

---

## 3. Изменения в ace.py

### 3.1 Новый класс ACEDevice

**Файл:** `extras/ace.py`

**Добавить после импортов (строка ~17):**

```python
class ACEDevice:
    """
    Представляет одно физическое ACE устройство с 4 слотами.
    Инкапсулирует serial-соединение и состояние устройства.
    """
    
    def __init__(self, device_id: int, config, parent_logger, reactor):
        self.device_id = device_id
        self.reactor = reactor
        self.logger = logging.getLogger(f'ace.device_{device_id}')
        
        # Serial configuration
        self.serial_name = config.get(f'serial_{device_id}', None)
        self.baud = config.getint('baud', 115200)
        
        # Timeouts
        self._response_timeout = config.getfloat('response_timeout', 2.0)
        self._read_timeout = config.getfloat('read_timeout', 0.1)
        self._write_timeout = config.getfloat('write_timeout', 0.5)
        self._max_queue_size = config.getint('max_queue_size', 20)
        
        # Connection state
        self._serial = None
        self._connected = False
        self._manually_disconnected = False
        self._connection_attempts = 0
        self._max_connection_attempts = 5
        
        # Device state
        self._info = self._get_default_info()
        self._callback_map = {}
        self._request_id = 0
        
        # Queues and buffers
        self._queue = queue.Queue(maxsize=self._max_queue_size)
        self.read_buffer = bytearray()
        
        # Timers
        self._reader_timer = None
        self._writer_timer = None
        self._last_status_request = 0
        
        # Parking state (per-device)
        self._park_in_progress = False
        self._park_error = False
        self._park_index = -1  # Local slot index 0-3
        self._park_start_time = 0
        self._assist_hit_count = 0
        self._last_assist_count = 0
        self._park_count_increased = False
        
        # Feed assist state
        self._feed_assist_index = -1  # Local slot index 0-3
        
    def _get_default_info(self) -> Dict[str, Any]:
        return {
            'status': 'disconnected',
            'dryer': {
                'status': 'stop',
                'target_temp': 0,
                'duration': 0,
                'remain_time': 0
            },
            'temp': 0,
            'enable_rfid': 1,
            'fan_speed': 7000,
            'feed_assist_count': 0,
            'cont_assist_time': 0.0,
            'slots': [{
                'index': i,
                'status': 'empty',
                'sku': '',
                'type': '',
                'color': [0, 0, 0]
            } for i in range(4)]
        }
    
    def connect(self) -> bool:
        """Подключиться к устройству"""
        # ... реализация аналогична текущему _connect()
        
    def disconnect(self):
        """Отключиться от устройства"""
        # ... реализация аналогична текущему _disconnect()
        
    def send_request(self, request: Dict, callback: Callable):
        """Отправить запрос устройству"""
        # ... реализация аналогична текущему send_request()
        
    def get_slot_status(self, local_slot: int) -> Dict:
        """Получить статус локального слота (0-3)"""
        if 0 <= local_slot < 4:
            return self._info['slots'][local_slot]
        return None
    
    def get_status(self) -> Dict:
        """Получить полное состояние устройства"""
        return self._info.copy()
```

### 3.2 Изменения в классе ValgAce

**Изменить `__init__` (строки 25-136):**

```python
def __init__(self, config):
    self.printer = config.get_printer()
    self.toolhead = None
    self.reactor = self.printer.get_reactor()
    self.gcode = self.printer.lookup_object('gcode')
    
    self.logger = logging.getLogger('ace')
    self._name = 'ace'
    
    # === MULTI-DEVICE CONFIGURATION ===
    self.max_devices = config.getint('max_devices', 4)
    self.devices: Dict[int, ACEDevice] = {}
    
    # Initialize devices from config
    for device_id in range(self.max_devices):
        serial_key = f'serial_{device_id}' if device_id > 0 else 'serial'
        serial_port = config.get(serial_key, None)
        
        if serial_port:
            self.devices[device_id] = ACEDevice(
                device_id=device_id,
                config=config,
                parent_logger=self.logger,
                reactor=self.reactor
            )
    
    # Backward compatibility: single serial parameter
    if not self.devices and config.get('serial', None):
        self.devices[0] = ACEDevice(
            device_id=0,
            config=config,
            parent_logger=self.logger,
            reactor=self.reactor
        )
    
    # === GLOBAL SLOT MAPPING ===
    # Global slot 0-15 -> (device_id 0-3, local_slot 0-3)
    self._build_slot_mapping()
    
    # === COMMON CONFIGURATION ===
    self.feed_speed = config.getint('feed_speed', 50)
    self.retract_speed = config.getint('retract_speed', 50)
    # ... остальные параметры
    
    # === CURRENT TOOL TRACKING ===
    self._current_global_slot = -1  # -1 = no tool selected
    
    # ... остальная инициализация
```

### 3.3 Новые методы для работы с глобальными слотами

**Добавить в класс ValgAce:**

```python
def _build_slot_mapping(self):
    """
    Построить отображение глобальных слотов на устройства.
    Глобальный слот = device_id * 4 + local_slot
    """
    self._slot_to_device: Dict[int, Tuple[int, int]] = {}
    self._device_slot_to_global: Dict[Tuple[int, int], int] = {}
    
    global_slot = 0
    for device_id in sorted(self.devices.keys()):
        for local_slot in range(4):
            self._slot_to_device[global_slot] = (device_id, local_slot)
            self._device_slot_to_global[(device_id, local_slot)] = global_slot
            global_slot += 1
    
    self._total_slots = global_slot

def get_device_and_slot(self, global_slot: int) -> Tuple[ACEDevice, int]:
    """
    Преобразовать глобальный индекс слота в устройство и локальный слот.
    
    Args:
        global_slot: Глобальный индекс слота (0-15)
    
    Returns:
        Tuple[ACEDevice, local_slot] или (None, -1) если не найден
    """
    if global_slot not in self._slot_to_device:
        self.logger.error(f"Invalid global slot: {global_slot}")
        return None, -1
    
    device_id, local_slot = self._slot_to_device[global_slot]
    device = self.devices.get(device_id)
    
    if not device:
        self.logger.error(f"Device {device_id} not found for slot {global_slot}")
        return None, -1
    
    return device, local_slot

def get_global_slot(self, device_id: int, local_slot: int) -> int:
    """
    Преобразовать device_id + local_slot в глобальный индекс.
    """
    return self._device_slot_to_global.get((device_id, local_slot), -1)
```

### 3.4 Изменения в G-code командах

**Изменить `cmd_ACE_CHANGE_TOOL` (строки 1163-1274):**

```python
def cmd_ACE_CHANGE_TOOL(self, gcmd):
    """
    Смена инструмента с поддержкой глобальных слотов 0-15.
    Обратная совместимость: слоты 0-3 работают как раньше.
    """
    tool = gcmd.get_int('TOOL', minval=-1, maxval=self._total_slots - 1)
    was = self._current_global_slot
    
    if was == tool:
        gcmd.respond_info(f"Tool already set to {tool}")
        return
    
    # Получаем устройство и локальный слот для нового инструмента
    if tool != -1:
        device, local_slot = self.get_device_and_slot(tool)
        if not device:
            gcmd.respond_raw(f"ACE Error: Invalid tool {tool}")
            return
        
        if not device._connected:
            gcmd.respond_raw(f"ACE Error: Device for tool {tool} not connected")
            return
        
        if device.get_slot_status(local_slot).get('status') != 'ready':
            self.gcode.run_script_from_command(f"_ACE_ON_EMPTY_ERROR INDEX={tool}")
            return
    
    # Получаем устройство для предыдущего инструмента
    if was != -1:
        prev_device, prev_local_slot = self.get_device_and_slot(was)
    else:
        prev_device, prev_local_slot = None, -1
    
    # Выполняем pre-toolchange макрос
    self.gcode.run_script_from_command(f"_ACE_PRE_TOOLCHANGE FROM={was} TO={tool}")
    
    # Обновляем текущий инструмент
    self._current_global_slot = tool
    self._save_variable('ace_current_index', tool)
    
    # === RETRACT PREVIOUS TOOL ===
    if prev_device and prev_local_slot >= 0:
        prev_device.send_request({
            "method": "unwind_filament",
            "params": {
                "index": prev_local_slot,
                "length": self.toolchange_retract_length,
                "speed": self.retract_speed
            }
        }, lambda r: None)
        
        # Wait for retract
        retract_time = (self.toolchange_retract_length / self.retract_speed) + 1.0
        if self.toolhead:
            self.toolhead.dwell(retract_time)
    
    # === PARK NEW TOOL ===
    if tool != -1:
        device._park_to_toolhead(local_slot)
        
        # Wait for parking
        timeout = self.reactor.monotonic() + 60.0
        while device._park_in_progress:
            if device._park_error:
                gcmd.respond_raw(f"ACE Error: Parking failed for tool {tool}")
                return
            if self.reactor.monotonic() > timeout:
                gcmd.respond_raw("ACE Error: Parking timeout")
                return
            if self.toolhead:
                self.toolhead.dwell(1.0)
    
    # Post-toolchange
    self.gcode.run_script_from_command(f'_ACE_POST_TOOLCHANGE FROM={was} TO={tool}')
    gcmd.respond_info(f"Tool changed from {was} to {tool}")
```

### 3.5 Изменения в get_status для Moonraker

**Изменить `get_status` (строки 335-381):**

```python
def get_status(self, eventtime):
    """
    Возвращает статус для Moonraker API.
    Поддерживает как одиночное устройство (обратная совместимость),
    так и массив устройств.
    """
    # Single device mode (backward compatible)
    if len(self.devices) == 1:
        device = self.devices[0]
        return self._get_single_device_status(device, eventtime)
    
    # Multi-device mode
    devices_status = {}
    all_slots = []
    
    for device_id, device in self.devices.items():
        device_status = self._get_single_device_status(device, eventtime)
        devices_status[device_id] = device_status
        
        # Add device_id to each slot
        for slot in device_status.get('slots', []):
            slot_copy = slot.copy()
            slot_copy['device_id'] = device_id
            global_slot = self.get_global_slot(device_id, slot['index'])
            slot_copy['global_index'] = global_slot
            all_slots.append(slot_copy)
    
    return {
        'mode': 'multi_device',
        'total_devices': len(self.devices),
        'total_slots': self._total_slots,
        'current_tool': self._current_global_slot,
        'devices': devices_status,
        'slots': all_slots,  # Flat list of all 16 slots
        'filament_sensor': self._get_filament_sensor_status(eventtime)
    }

def _get_single_device_status(self, device: ACEDevice, eventtime) -> Dict:
    """Получить статус одного устройства (для обратной совместимости)"""
    info = device._info
    dryer_data = info.get('dryer', {}) or info.get('dryer_status', {})
    
    # Normalize time
    if isinstance(dryer_data, dict):
        dryer_normalized = dryer_data.copy()
        remain_time_raw = dryer_normalized.get('remain_time', 0)
        if remain_time_raw > 0:
            dryer_normalized['remain_time'] = remain_time_raw / 60
    else:
        dryer_normalized = {}
    
    return {
        'status': info.get('status', 'unknown'),
        'model': info.get('model', ''),
        'firmware': info.get('firmware', ''),
        'temp': info.get('temp', 0),
        'fan_speed': info.get('fan_speed', 0),
        'dryer': dryer_normalized,
        'slots': info.get('slots', []),
        'connected': device._connected,
        'feed_assist_slot': device._feed_assist_index
    }
```

### 3.6 Сводная таблица изменений в ace.py

| Метод/Атрибут | Изменение |
|---------------|-----------|
| `__init__` | Инициализация массива устройств |
| `_connect` | Перенос в `ACEDevice.connect()` |
| `_disconnect` | Перенос в `ACEDevice.disconnect()` |
| `send_request` | Перенос в `ACEDevice.send_request()` |
| `_reader_loop` | Перенос в `ACEDevice._reader_loop()` |
| `_writer_loop` | Перенос в `ACEDevice._writer_loop()` |
| `_park_to_toolhead` | Перенос в `ACEDevice._park_to_toolhead()` |
| `get_status` | Поддержка multi-device режима |
| `cmd_ACE_CHANGE_TOOL` | Глобальные слоты 0-15 |
| `cmd_ACE_STATUS` | Статус всех устройств |
| `cmd_ACE_FEED` | Глобальные слоты |
| `cmd_ACE_RETRACT` | Глобальные слоты |
| `cmd_ACE_ENABLE_FEED_ASSIST` | Глобальные слоты |
| `cmd_ACE_PARK_TO_TOOLHEAD` | Глобальные слоты |
| `_info` | Перенос в `ACEDevice._info` |
| `_connected` | Перенос в `ACEDevice._connected` |
| `_queue` | Перенос в `ACEDevice._queue` |

---

## 4. Изменения в ace.cfg

### 4.1 Новая структура конфигурации

**Текущий формат (одно устройство):**
```ini
[ace]
serial: /dev/serial/by-id/usb-ANYCUBIC_ACE_1-if00
baud: 115200
```

**Новый формат (множественные устройства):**
```ini
[ace]
# === Device Configuration ===
# Maximum number of devices (1-4)
max_devices: 4

# Device 0 (primary) - backward compatible 'serial' parameter
serial: /dev/serial/by-id/usb-ANYCUBIC_ACE_1-if00

# Additional devices (optional)
serial_1: /dev/serial/by-id/usb-ANYCUBIC_ACE_2-if00
serial_2: /dev/serial/by-id/usb-ANYCUBIC_ACE_3-if00
serial_3: /dev/serial/by-id/usb-ANYCUBIC_ACE_4-if00

# Common baud rate for all devices
baud: 115200

# === Common Parameters ===
feed_speed: 25
retract_speed: 25
retract_mode: 0
toolchange_retract_length: 100
park_hit_count: 5
max_dryer_temperature: 55
disable_assist_after_toolchange: False
infinity_spool_mode: False
aggressive_parking: False
max_parking_distance: 100
parking_speed: 10

# === Optional: Per-Device Overrides ===
# device_0_feed_speed: 30
# device_1_max_dryer_temperature: 60
```

### 4.2 Новые макросы для 16 инструментов

**Добавить в ace.cfg:**

```ini
# === Tool Change Macros (T0-T15) ===
# T-1 = Unload all tools
[gcode_macro TR]
gcode:
    ACE_CHANGE_TOOL TOOL=-1

# Device 0, Slots 0-3
[gcode_macro T0]
gcode:
    ACE_CHANGE_TOOL TOOL=0

[gcode_macro T1]
gcode:
    ACE_CHANGE_TOOL TOOL=1

[gcode_macro T2]
gcode:
    ACE_CHANGE_TOOL TOOL=2

[gcode_macro T3]
gcode:
    ACE_CHANGE_TOOL TOOL=3

# Device 1, Slots 0-3 (Global 4-7)
[gcode_macro T4]
gcode:
    ACE_CHANGE_TOOL TOOL=4

[gcode_macro T5]
gcode:
    ACE_CHANGE_TOOL TOOL=5

[gcode_macro T6]
gcode:
    ACE_CHANGE_TOOL TOOL=6

[gcode_macro T7]
gcode:
    ACE_CHANGE_TOOL TOOL=7

# Device 2, Slots 0-3 (Global 8-11)
[gcode_macro T8]
gcode:
    ACE_CHANGE_TOOL TOOL=8

[gcode_macro T9]
gcode:
    ACE_CHANGE_TOOL TOOL=9

[gcode_macro T10]
gcode:
    ACE_CHANGE_TOOL TOOL=10

[gcode_macro T11]
gcode:
    ACE_CHANGE_TOOL TOOL=11

# Device 3, Slots 0-3 (Global 12-15)
[gcode_macro T12]
gcode:
    ACE_CHANGE_TOOL TOOL=12

[gcode_macro T13]
gcode:
    ACE_CHANGE_TOOL TOOL=13

[gcode_macro T14]
gcode:
    ACE_CHANGE_TOOL TOOL=14

[gcode_macro T15]
gcode:
    ACE_CHANGE_TOOL TOOL=15
```

### 4.3 Обновленные макросы с глобальными слотами

```ini
[gcode_macro PARK_TO_TOOLHEAD]
gcode:
    {% if params.INDEX is defined %}
        {% set target_index = params.INDEX|int %}
        M118 Запущена парковка филамента слот {target_index}.
        ACE_PARK_TO_TOOLHEAD INDEX={target_index}
    {% else %}
        {action_respond_info("Index is lost")}
        RESPOND TYPE=error MSG="Error INDEX is lost"
    {% endif %}

[gcode_macro START_DRYING]
gcode:
    # Добавлен параметр DEVICE для указания устройства
    {% set device_id = params.DEVICE|default(0)|int %}
    {% set target_temp = params.TEMP|default(55)|int %}
    {% set target_time = params.TIME|default(120)|int %}
    
    M118 Запущена сушка устройства {device_id}: {target_temp}°C, {target_time} мин.
    ACE_START_DRYING DEVICE={device_id} TEMP={target_temp} DURATION={target_time}
```

### 4.4 Новые G-code команды

**Добавить в ace.py регистрацию команд:**

```python
commands = [
    # ... существующие команды ...
    
    # Новые команды для multi-device
    ('ACE_DEVICE_STATUS', self.cmd_ACE_DEVICE_STATUS, "Get specific device status"),
    ('ACE_DEVICE_CONNECT', self.cmd_ACE_DEVICE_CONNECT, "Connect specific device"),
    ('ACE_DEVICE_DISCONNECT', self.cmd_ACE_DEVICE_DISCONNECT, "Disconnect specific device"),
    ('ACE_LIST_DEVICES', self.cmd_ACE_LIST_DEVICES, "List all configured devices"),
]
```

---

## 5. Схема адресации 16 слотов

### 5.1 Математика адресации

```
Глобальный слот (0-15) → device_id, local_slot

device_id = global_slot // 4
local_slot = global_slot % 4

Обратно:
global_slot = device_id * 4 + local_slot
```

### 5.2 Таблица соответствия

| Глобальный слот | Device ID | Local Slot | Примечание |
|-----------------|-----------|------------|------------|
| 0 | 0 | 0 | Устройство 0, Слот 0 |
| 1 | 0 | 1 | Устройство 0, Слот 1 |
| 2 | 0 | 2 | Устройство 0, Слот 2 |
| 3 | 0 | 3 | Устройство 0, Слот 3 |
| 4 | 1 | 0 | Устройство 1, Слот 0 |
| 5 | 1 | 1 | Устройство 1, Слот 1 |
| 6 | 1 | 2 | Устройство 1, Слот 2 |
| 7 | 1 | 3 | Устройство 1, Слот 3 |
| 8 | 2 | 0 | Устройство 2, Слот 0 |
| 9 | 2 | 1 | Устройство 2, Слот 1 |
| 10 | 2 | 2 | Устройство 2, Слот 2 |
| 11 | 2 | 3 | Устройство 2, Слот 3 |
| 12 | 3 | 0 | Устройство 3, Слот 0 |
| 13 | 3 | 1 | Устройство 3, Слот 1 |
| 14 | 3 | 2 | Устройство 3, Слот 2 |
| 15 | 3 | 3 | Устройство 3, Слот 3 |

### 5.3 Визуализация

```
┌─────────────────────────────────────────────────────────────────┐
│                     ValgAce Manager                              │
│  current_tool: 5 (Device 1, Slot 1)                             │
└─────────────────────────────────────────────────────────────────┘
         │              │              │              │
         ▼              ▼              ▼              ▼
┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
│  Device 0   │ │  Device 1   │ │  Device 2   │ │  Device 3   │
│  /dev/ACE1  │ │  /dev/ACE2  │ │  /dev/ACE3  │ │  /dev/ACE4  │
├─────────────┤ ├─────────────┤ ├─────────────┤ ├─────────────┤
│ Slot 0 (G0) │ │ Slot 0 (G4) │ │ Slot 0 (G8) │ │ Slot 0(G12) │
│ Slot 1 (G1) │ │ Slot 1 (G5) │ │ Slot 1 (G9) │ │ Slot 1(G13) │
│ Slot 2 (G2) │ │ Slot 2 (G6) │ │ Slot 2(G10) │ │ Slot 2(G14) │
│ Slot 3 (G3) │ │ Slot 3 (G7) │ │ Slot 3(G11) │ │ Slot 3(G15) │
└─────────────┘ └─────────────┘ └─────────────┘ └─────────────┘
```

### 5.4 API для работы с адресацией

```python
class ValgAce:
    # ... 
    
    def global_to_local(self, global_slot: int) -> Tuple[int, int]:
        """Конвертировать глобальный слот в (device_id, local_slot)"""
        if global_slot < 0 or global_slot >= self._total_slots:
            raise ValueError(f"Invalid global slot: {global_slot}")
        return global_slot // 4, global_slot % 4
    
    def local_to_global(self, device_id: int, local_slot: int) -> int:
        """Конвертировать (device_id, local_slot) в глобальный слот"""
        if device_id not in self.devices:
            raise ValueError(f"Invalid device_id: {device_id}")
        if local_slot < 0 or local_slot > 3:
            raise ValueError(f"Invalid local_slot: {local_slot}")
        return device_id * 4 + local_slot
    
    def get_slot_info(self, global_slot: int) -> Dict:
        """Получить полную информацию о слоте по глобальному индексу"""
        device_id, local_slot = self.global_to_local(global_slot)
        device = self.devices.get(device_id)
        
        if not device or not device._connected:
            return {'error': 'Device not connected'}
        
        slot_info = device.get_slot_status(local_slot)
        slot_info['device_id'] = device_id
        slot_info['global_index'] = global_slot
        
        return slot_info
```

---

## 6. Обработка ошибок и таймаутов

### 6.1 Иерархия ошибок

```python
class ACEError(Exception):
    """Базовое исключение для ACE модуля"""
    pass

class ACEDeviceNotFoundError(ACEError):
    """Устройство не найдено"""
    pass

class ACEDeviceDisconnectedError(ACEError):
    """Устройство отключено"""
    pass

class ACESlotNotReadyError(ACEError):
    """Слот не готов к операции"""
    pass

class ACETimeoutError(ACEError):
    """Таймаут операции"""
    pass

class ACEParkingError(ACEError):
    """Ошибка парковки"""
    pass
```

### 6.2 Обработка ошибок на уровне устройства

```python
class ACEDevice:
    # ...
    
    def send_request_safe(self, request: Dict, callback: Callable, 
                          timeout: float = None) -> bool:
        """
        Безопасная отправка запроса с обработкой ошибок.
        
        Returns:
            True если запрос отправлен, False при ошибке
        """
        if not self._connected:
            self.logger.error(f"Device {self.device_id} not connected")
            if callback:
                callback({'error': 'Device not connected', 'code': -1})
            return False
        
        try:
            self.send_request(request, callback)
            return True
        except queue.Full:
            self.logger.error(f"Device {self.device_id} queue full")
            if callback:
                callback({'error': 'Queue full', 'code': -2})
            return False
        except Exception as e:
            self.logger.error(f"Device {self.device_id} send error: {e}")
            if callback:
                callback({'error': str(e), 'code': -3})
            return False
    
    def wait_for_connection(self, timeout: float = 10.0) -> bool:
        """
        Ожидать подключения устройства с таймаутом.
        """
        start = time.monotonic()
        while not self._connected:
            if time.monotonic() - start > timeout:
                return False
            time.sleep(0.1)
        return True
```

### 6.3 Обработка ошибок на уровне менеджера

```python
class ValgAce:
    # ...
    
    def execute_on_device(self, global_slot: int, method: str, 
                          params: Dict, callback: Callable = None) -> bool:
        """
        Выполнить метод на устройстве, соответствующем слоту.
        
        Returns:
            True если запрос отправлен, False при ошибке
        """
        device, local_slot = self.get_device_and_slot(global_slot)
        
        if not device:
            self.logger.error(f"No device for slot {global_slot}")
            if callback:
                callback({'error': f'No device for slot {global_slot}'})
            return False
        
        if not device._connected:
            self.logger.error(f"Device {device.device_id} not connected")
            if callback:
                callback({'error': f'Device {device.device_id} not connected'})
            return False
        
        # Add local slot index to params
        params_with_slot = params.copy()
        params_with_slot['index'] = local_slot
        
        return device.send_request_safe(
            {'method': method, 'params': params_with_slot},
            callback
        )
    
    def broadcast_to_all_devices(self, method: str, params: Dict = None):
        """
        Отправить команду всем подключенным устройствам.
        """
        for device_id, device in self.devices.items():
            if device._connected:
                device.send_request_safe(
                    {'method': method, 'params': params or {}},
                    lambda r: None
                )
```

### 6.4 Таймауты операций

```python
# Константы таймаутов
TIMEOUTS = {
    'connection': 10.0,        # Подключение к устройству
    'reconnect': 5.0,          # Переподключение
    'status_request': 2.0,     # Запрос статуса
    'feed_operation': 60.0,    # Операция подачи
    'retract_operation': 30.0, # Операция втягивания
    'parking': 60.0,           # Парковка к соплу
    'toolchange': 120.0,       # Полная смена инструмента
    'drying_start': 5.0,       # Запуск сушки
}

class ACEDevice:
    def execute_with_timeout(self, operation: Callable, 
                             timeout: float, 
                             on_timeout: Callable = None) -> bool:
        """
        Выполнить операцию с таймаутом.
        """
        start = self.reactor.monotonic()
        result = {'completed': False, 'error': None}
        
        def check_timeout(eventtime):
            if result['completed']:
                return self.reactor.NEVER
            
            if eventtime - start > timeout:
                self.logger.error(f"Operation timeout after {timeout}s")
                if on_timeout:
                    on_timeout()
                result['error'] = 'timeout'
                return self.reactor.NEVER
            
            return eventtime + 0.1
        
        self.reactor.register_timer(check_timeout, self.reactor.NOW)
        
        try:
            operation()
            result['completed'] = True
            return True
        except Exception as e:
            result['error'] = str(e)
            return False
```

### 6.5 Восстановление после ошибок

```python
class ACEDevice:
    def handle_connection_error(self, error: Exception):
        """
        Обработать ошибку соединения и попытаться восстановить.
        """
        self.logger.error(f"Device {self.device_id} connection error: {error}")
        
        # Mark as disconnected
        self._connected = False
        self._info['status'] = 'error'
        
        # Clear pending operations
        self._clear_pending_operations()
        
        # Schedule reconnect
        self.reactor.register_timer(
            lambda e: self._reconnect(),
            self.reactor.monotonic() + 1.0
        )
    
    def _clear_pending_operations(self):
        """Очистить ожидающие операции при разрыве соединения"""
        # Clear queue
        while not self._queue.empty():
            try:
                _, callback = self._queue.get_nowait()
                if callback:
                    callback({'error': 'Connection lost', 'code': -100})
            except:
                pass
        
        # Clear callback map
        for request_id, callback in self._callback_map.items():
            try:
                callback({'error': 'Connection lost', 'code': -100})
            except:
                pass
        self._callback_map.clear()
        
        # Reset parking state
        self._park_in_progress = False
        self._park_error = True
        self._feed_assist_index = -1
```

---

## 7. Обратная совместимость

### 7.1 Принципы обратной совместимости

1. **Конфигурация**: Старый формат `serial: ...` работает как раньше
2. **G-code команды**: `T0-T3` работают с устройством 0
3. **API Moonraker**: `get_status()` возвращает совместимую структуру
4. **Переменные**: `ace_current_index` хранит глобальный слот

### 7.2 Определение режима работы

```python
class ValgAce:
    def _detect_mode(self, config) -> str:
        """
        Определить режим работы: single или multi_device.
        """
        # Check for multiple serial ports
        has_serial = config.get('serial', None) is not None
        has_serial_1 = config.get('serial_1', None) is not None
        has_serial_2 = config.get('serial_2', None) is not None
        has_serial_3 = config.get('serial_3', None) is not None
        
        if has_serial_1 or has_serial_2 or has_serial_3:
            return 'multi_device'
        elif has_serial:
            return 'single'
        else:
            raise config.error("No serial port configured for ACE")
    
    def is_single_device_mode(self) -> bool:
        """Проверить, работает ли модуль в режиме одного устройства"""
        return len(self.devices) == 1
```

### 7.3 Совместимый get_status для single режима

```python
def get_status(self, eventtime):
    """
    Возвращает статус в формате, совместимом с текущей версией.
    """
    if self.is_single_device_mode():
        # Single device mode - return current format
        device = self.devices[0]
        return {
            'status': device._info.get('status', 'unknown'),
            'model': device._info.get('model', ''),
            'firmware': device._info.get('firmware', ''),
            'temp': device._info.get('temp', 0),
            'fan_speed': device._info.get('fan_speed', 0),
            'dryer': device._info.get('dryer', {}),
            'slots': device._info.get('slots', []),
            'feed_assist_slot': device._feed_assist_index,
            # ... остальные поля как в текущей версии
        }
    else:
        # Multi-device mode - extended format
        return self._get_multi_device_status(eventtime)
```

### 7.4 Совместимые G-code команды

```python
def cmd_ACE_ENABLE_FEED_ASSIST(self, gcmd):
    """
    Включить вспомогательную подачу.
    INDEX может быть 0-3 (local) или 0-15 (global).
    """
    index = gcmd.get_int('INDEX', minval=0, maxval=15)
    
    # В single режиме INDEX 0-3 работает как раньше
    if self.is_single_device_mode() and index <= 3:
        device = self.devices[0]
        local_slot = index
    else:
        device, local_slot = self.get_device_and_slot(index)
    
    if not device:
        gcmd.respond_raw(f"ACE Error: Invalid slot {index}")
        return
    
    def callback(response):
        if response.get('code', 0) != 0:
            gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        else:
            device._feed_assist_index = local_slot
            gcmd.respond_info(f"Feed assist enabled for slot {index}")
    
    device.send_request_safe(
        {"method": "start_feed_assist", "params": {"index": local_slot}},
        callback
    )
```

### 7.5 Миграция переменных

```python
def _migrate_variables(self):
    """
    Миграция переменных от старой версии к новой.
    """
    # Старая переменная: ace_current_index (0-3)
    # Новая переменная: ace_current_global_slot (0-15)
    
    old_index = self.variables.get('ace_current_index', -1)
    new_global = self.variables.get('ace_current_global_slot', None)
    
    if new_global is None and old_index >= 0:
        # Миграция: старый индекс 0-3 становится глобальным 0-3
        self._current_global_slot = old_index
        self._save_variable('ace_current_global_slot', old_index)
        self.logger.info(f"Migrated ace_current_index {old_index} to global slot")
    else:
        self._current_global_slot = new_global if new_global is not None else -1
```

---

## 8. Этапы реализации

### 8.1 Фаза 1: Рефакторинг (без изменения функциональности)

**Задачи:**
1. Создать класс `ACEDevice` с переносом логики одного устройства
2. Изменить `ValgAce` для использования `ACEDevice` (режим single)
3. Добавить тесты для проверки идентичного поведения
4. Обновить документацию

**Файлы:**
- `extras/ace.py` - основной рефакторинг
- `tests/test_ace_device.py` - новые тесты

**Критерий готовности:**
- Все существующие тесты проходят
- Режим single device работает идентично текущей версии

### 8.2 Фаза 2: Multi-device инфраструктура

**Задачи:**
1. Реализовать массив устройств в `ValgAce`
2. Добавить схему адресации глобальных слотов
3. Реализовать конфигурацию множественных serial портов
4. Добавить таймеры для каждого устройства

**Файлы:**
- `extras/ace.py` - поддержка массива устройств
- `ace.cfg` - пример конфигурации multi-device

**Критерий готовности:**
- Модуль инициализирует до 4 устройств
- Каждое устройство независимо подключается/отключается

### 8.3 Фаза 3: G-code команды для multi-device

**Задачи:**
1. Обновить `cmd_ACE_CHANGE_TOOL` для глобальных слотов
2. Обновить все команды с параметром INDEX
3. Добавить новые команды: `ACE_DEVICE_STATUS`, `ACE_LIST_DEVICES`
4. Обновить макросы `T0-T15`

**Файлы:**
- `extras/ace.py` - обновление команд
- `ace.cfg` - макросы T0-T15

**Критерий готовности:**
- Команды `T0-T15` работают корректно
- Обратная совместимость с `T0-T3` сохранена

### 8.4 Фаза 4: API Moonraker и интеграция

**Задачи:**
1. Обновить `get_status()` для multi-device режима
2. Добавить WebSocket события для изменения статуса устройств
3. Интеграция с Fluidd/Mainsail UI
4. Тестирование с реальными устройствами

**Файлы:**
- `extras/ace.py` - обновление API
- `docs/API.md` - документация API

**Критерий готовности:**
- Moonraker корректно отображает статус всех устройств
- UI показывает 16 слотов

### 8.5 Фаза 5: Обработка ошибок и тестирование

**Задачи:**
1. Реализовать иерархию исключений
2. Добавить recovery механизмы
3. Написать unit-тесты для всех компонентов
4. Интеграционное тестирование с mock устройствами

**Файлы:**
- `extras/ace.py` - обработка ошибок
- `tests/` - тесты

**Критерий готовности:**
- Все edge cases обработаны
- Тестовое покрытие > 80%

### 8.6 Фаза 6: Документация и релиз

**Задачи:**
1. Обновить `README.md`
2. Создать `docs/MIGRATION.md` для перехода с single на multi
3. Обновить `docs/Protocol.md` если нужно
4. Подготовить release notes

**Файлы:**
- `README.md`
- `docs/MIGRATION.md`
- `docs/Protocol.md`
- `CHANGELOG.md`

---

## Приложение A: Полный список изменений

### A.1 Новые классы

| Класс | Описание |
|-------|----------|
| `ACEDevice` | Представляет одно физическое устройство |
| `ACEError` | Базовое исключение |
| `ACEDeviceNotFoundError` | Устройство не найдено |
| `ACEDeviceDisconnectedError` | Устройство отключено |
| `ACESlotNotReadyError` | Слот не готов |
| `ACETimeoutError` | Таймаут операции |

### A.2 Новые методы ValgAce

| Метод | Описание |
|-------|----------|
| `_detect_mode()` | Определить режим single/multi |
| `_build_slot_mapping()` | Построить таблицу слотов |
| `get_device_and_slot()` | Конвертировать global → (device, local) |
| `get_global_slot()` | Конвертировать (device, local) → global |
| `execute_on_device()` | Выполнить команду на устройстве |
| `broadcast_to_all_devices()` | Отправить команду всем устройствам |

### A.3 Новые G-code команды

| Команда | Описание |
|---------|----------|
| `ACE_DEVICE_STATUS` | Статус конкретного устройства |
| `ACE_DEVICE_CONNECT` | Подключить устройство |
| `ACE_DEVICE_DISCONNECT` | Отключить устройство |
| `ACE_LIST_DEVICES` | Список всех устройств |

### A.4 Новые параметры конфигурации

| Параметр | По умолчанию | Описание |
|----------|--------------|----------|
| `max_devices` | 4 | Максимальное количество устройств |
| `serial_1` | None | Serial порт устройства 1 |
| `serial_2` | None | Serial порт устройства 2 |
| `serial_3` | None | Serial порт устройства 3 |

---

## Приложение B: Примеры использования

### B.1 Конфигурация для 2 устройств

```ini
[ace]
max_devices: 2
serial: /dev/serial/by-id/usb-ANYCUBIC_ACE_1-if00
serial_1: /dev/serial/by-id/usb-ANYCUBIC_ACE_2-if00
baud: 115200
feed_speed: 25
retract_speed: 25
```

### B.2 Смена инструмента между устройствами

```gcode
; Текущий инструмент: T0 (Device 0, Slot 0)
; Переключение на T5 (Device 1, Slot 1)
T5

; Это автоматически:
; 1. Втягивает филамент из Device 0, Slot 0
; 2. Паркует филамент из Device 1, Slot 1 к соплу
```

### B.3 Запрос статуса всех устройств

```gcode
ACE_STATUS
; Выводит статус всех подключенных устройств

ACE_DEVICE_STATUS DEVICE=1
; Выводит статус только устройства 1
```

### B.4 Сушка на конкретном устройстве

```gcode
START_DRYING DEVICE=0 TEMP=50 TIME=120
; Запускает сушку на устройстве 0

START_DRYING DEVICE=2 TEMP=55 TIME=240
; Запускает сушку на устройстве 2
```

---

*Документ создан: 2026-02-14*
*Версия плана: 1.0*
