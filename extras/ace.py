# File: ace.py — ValgAce module for Klipper
# Multi-device support: Up to 4 ACE devices (16 slots total)

import logging
import json
import struct
import queue
from typing import Optional, Dict, Any, Callable, Tuple, List

# Check for required libraries and raise an error if they are not available
try:
    import serial
    from serial import SerialException
except ImportError:
    serial = None
    SerialException = Exception
    raise ImportError("The 'pyserial' library is required for ValgAce module. Please install it using 'pip install pyserial'")


# =============================================================================
# ACEDevice - Represents a single physical ACE device (4 slots)
# =============================================================================

class ACEDevice:
    """
    Представляет одно физическое ACE устройство с 4 слотами.
    Инкапсулирует serial-соединение и состояние устройства.
    
    Multi-device: Каждый экземпляр независим от других.
    """
    
    def __init__(self, device_id: int, config, reactor, parent_logger, gcode):
        """
        Инициализация устройства ACE.
        
        Args:
            device_id: Идентификатор устройства (0-3)
            config: Конфигурация Klipper
            reactor: Reactor Klipper
            parent_logger: Логгер родительского класса
            gcode: G-code обработчик
        """
        self.device_id = device_id
        self.reactor = reactor
        self.gcode = gcode
        
        # Создаём отдельный логгер для устройства
        self.logger = logging.getLogger(f'ace.device_{device_id}')
        
        # === Serial Configuration ===
        # Для device_id=0 используем параметр 'serial' (обратная совместимость)
        # Для device_id=1,2,3 используем 'serial_1', 'serial_2', 'serial_3'
        if device_id == 0:
            self.serial_name = config.get('serial', None)
        else:
            self.serial_name = config.get(f'serial_{device_id}', None)
        
        self.baud = config.getint('baud', 115200)
        
        # === Timeout Parameters ===
        self._response_timeout = config.getfloat('response_timeout', 2.0)
        self._read_timeout = config.getfloat('read_timeout', 0.1)
        self._write_timeout = config.getfloat('write_timeout', 0.5)
        self._max_queue_size = config.getint('max_queue_size', 20)
        
        # === Connection State ===
        self._serial = None
        self._connected = False
        self._manually_disconnected = False
        self._connection_attempts = 0
        self._max_connection_attempts = 5
        
        # === Device State ===
        self._info = self._get_default_info()
        self._callback_map = {}
        self._request_id = 0
        
        # === Queues and Buffers ===
        self._queue = queue.Queue(maxsize=self._max_queue_size)
        self.read_buffer = bytearray()
        
        # === Timers ===
        self._reader_timer = None
        self._writer_timer = None
        self._last_status_request = 0
        
        # === Parking State (per-device) ===
        self._park_in_progress = False
        self._park_error = False
        self._park_index = -1  # Local slot index 0-3
        self._park_start_time = 0
        self._assist_hit_count = 0
        self._last_assist_count = 0
        self._park_count_increased = False
        
        # === Feed Assist State ===
        self._feed_assist_index = -1  # Local slot index 0-3
        
        # === Dwell flag ===
        self._dwell_scheduled = False
        
        self.logger.info(f"ACEDevice {device_id} initialized, serial: {self.serial_name}")
    
    def _get_default_info(self) -> Dict[str, Any]:
        """Возвращает структуру состояния устройства по умолчанию."""
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
    
    # =========================================================================
    # Connection Management
    # =========================================================================
    
    def connect(self) -> bool:
        """
        Подключиться к устройству.
        
        Returns:
            True если подключение успешно, False иначе
        """
        if self._connected:
            return True
        
        if not self.serial_name:
            self.logger.warning(f"Device {self.device_id}: No serial port configured")
            return False
        
        # Ensure any existing connection is properly closed
        if self._serial and self._serial.is_open:
            try:
                self._serial.close()
            except:
                pass
            self._serial = None
        
        for attempt in range(self._max_connection_attempts):
            try:
                self.logger.info(f"Attempting to connect to ACE device {self.device_id} at {self.serial_name} (attempt {attempt + 1}/{self._max_connection_attempts})")
                
                self._serial = serial.Serial(
                    port=self.serial_name,
                    baudrate=self.baud,
                    timeout=0,
                    write_timeout=self._write_timeout
                )
                
                if self._serial.is_open:
                    self._connected = True
                    self._info['status'] = 'ready'
                    self.logger.info(f"Connected to ACE device {self.device_id} at {self.serial_name}")

                    def info_callback(response):
                        res = response.get('result', {})
                        self.logger.info(f"Device {self.device_id} info: {res.get('model', 'Unknown')} {res.get('firmware', 'Unknown')}")

                    self.send_request({"method": "get_info"}, info_callback)

                    # Register timers if not already registered
                    if self._reader_timer is None:
                        self._reader_timer = self.reactor.register_timer(self._reader_loop, self.reactor.NOW)
                    if self._writer_timer is None:
                        self._writer_timer = self.reactor.register_timer(self._writer_loop, self.reactor.NOW)
                        
                    self.logger.info(f"Device {self.device_id} connection established successfully")
                    return True
                else:
                    # Close the serial port if it wasn't opened properly
                    if self._serial:
                        self._serial.close()
                        self._serial = None
            except SerialException as e:
                self.logger.info(f"Device {self.device_id} connection attempt {attempt + 1} failed: {str(e)}")
                if self._serial:
                    try:
                        self._serial.close()
                    except:
                        pass
                    self._serial = None
                self.dwell(1.0, lambda: None)
            except Exception as e:
                self.logger.error(f"Device {self.device_id} unexpected error during connection: {str(e)}")
                if self._serial:
                    try:
                        self._serial.close()
                    except:
                        pass
                    self._serial = None
                self.dwell(1.0, lambda: None)
        
        self.logger.warning(f"Failed to connect to ACE device {self.device_id}")
        return False
    
    def disconnect(self):
        """Gracefully disconnect from the device and stop all timers."""
        if not self._connected:
            return
        
        self.logger.info(f"Disconnecting from ACE device {self.device_id}...")
        
        # Stop all timers
        if self._reader_timer:
            self.reactor.unregister_timer(self._reader_timer)
            self._reader_timer = None
        if self._writer_timer:
            self.reactor.unregister_timer(self._writer_timer)
            self._writer_timer = None
        
        # Close serial connection
        try:
            if self._serial and self._serial.is_open:
                self._serial.close()
        except Exception as e:
            self.logger.error(f"Device {self.device_id} error closing serial connection: {str(e)}")
        finally:
            self._serial = None
        
        # Update connection status
        self._connected = False
        self._info['status'] = 'disconnected'
        
        # Clear any pending requests
        self._clear_pending_operations()
        
        self.logger.info(f"ACE device {self.device_id} disconnected successfully")
    
    def _clear_pending_operations(self):
        """Очистить ожидающие операции при разрыве соединения."""
        # Clear queue
        while not self._queue.empty():
            try:
                _, callback = self._queue.get_nowait()
                if callback:
                    try:
                        callback({'error': 'Connection lost', 'code': -100})
                    except:
                        pass
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
    
    def reconnect(self):
        """Переподключение к устройству."""
        self._manually_disconnected = False
        self.disconnect()
        self.dwell(1.0, lambda: None)
        self.connect()
    
    # =========================================================================
    # Communication
    # =========================================================================
    
    def _calc_crc(self, buffer: bytes) -> int:
        """
        Вычисление CRC для буфера данных.
        
        Args:
            buffer: Байтовый буфер для вычисления CRC
        Returns:
            Значение CRC
        """
        crc = 0xffff
        for byte in buffer:
            data = byte ^ (crc & 0xff)
            data ^= (data & 0x0f) << 4
            crc = (((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)) & 0xffff
        return crc & 0xffff
    
    def send_request(self, request: Dict[str, Any], callback: Callable):
        """
        Добавить запрос в очередь устройства.
        
        Args:
            request: JSON-RPC запрос
            callback: Функция обратного вызова для ответа
        """
        if self._queue.qsize() >= self._max_queue_size:
            self.logger.warning(f"Device {self.device_id} request queue overflow, clearing...")
            while not self._queue.empty():
                _, cb = self._queue.get_nowait()
                if cb:
                    try:
                        cb({'error': 'Queue overflow'})
                    except:
                        pass
        request['id'] = self._get_next_request_id()
        self._queue.put((request, callback))
    
    def send_request_safe(self, request: Dict, callback: Callable = None) -> bool:
        """
        Безопасная отправка запроса с обработкой ошибок.
        
        Args:
            request: JSON-RPC запрос
            callback: Функция обратного вызова
        Returns:
            True если запрос отправлен, False при ошибке
        """
        if not self._connected:
            self.logger.error(f"Device {self.device_id} not connected")
            if callback:
                callback({'error': f'Device {self.device_id} not connected', 'code': -1})
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
    
    def _get_next_request_id(self) -> int:
        """Получить следующий ID запроса."""
        self._request_id += 1
        if self._request_id >= 300000:
            self._request_id = 0
        return self._request_id
    
    def _send_request(self, request: Dict[str, Any]) -> bool:
        """Отправить запрос через serial порт."""
        try:
            payload = json.dumps(request).encode('utf-8')
        except Exception as e:
            self.logger.error(f"Device {self.device_id} JSON encoding error: {str(e)}")
            return False

        crc = self._calc_crc(payload)
        packet = (
            bytes([0xFF, 0xAA]) +
            struct.pack('<H', len(payload)) +
            payload +
            struct.pack('<H', crc) +
            bytes([0xFE])
        )

        try:
            if self._serial and self._serial.is_open:
                self._serial.write(packet)
                return True
            else:
                raise SerialException("Serial port closed")
        except SerialException as e:
            self.logger.error(f"Device {self.device_id} send error: {str(e)}")
            self.reconnect()
            return False
    
    # =========================================================================
    # Reader/Writer Loops
    # =========================================================================
    
    def _reader_loop(self, eventtime):
        """Цикл чтения данных из serial порта."""
        if not self._connected or not self._serial or not self._serial.is_open:
            return eventtime + 0.01
        try:
            raw_bytes = self._serial.read(16)
            if raw_bytes:
                self.read_buffer.extend(raw_bytes)
                self._process_messages()
        except SerialException as e:
            self.logger.error(f"Device {self.device_id} read error: {str(e)}")
            self.reconnect()
        return eventtime + 0.01
    
    def _process_messages(self):
        """Обработка входящих сообщений."""
        incomplete_message_count = 0
        max_incomplete_messages_before_reset = 10
        while self.read_buffer:
            end_idx = self.read_buffer.find(b'\xfe')
            if end_idx == -1:
                break
            msg = self.read_buffer[:end_idx+1]
            self.read_buffer = self.read_buffer[end_idx+1:]
            if len(msg) < 7 or msg[0:2] != bytes([0xFF, 0xAA]):
                continue
            payload_len = struct.unpack('<H', msg[2:4])[0]
            expected_length = 4 + payload_len + 3
            if len(msg) < expected_length:
                self.logger.debug(f"Device {self.device_id} incomplete message (expected {expected_length}, got {len(msg)})")
                incomplete_message_count += 1
                if incomplete_message_count > max_incomplete_messages_before_reset:
                    self.logger.warning(f"Device {self.device_id} too many incomplete messages, resetting connection")
                    self.reconnect()
                    incomplete_message_count = 0
                continue
            incomplete_message_count = 0
            payload = msg[4:4+payload_len]
            crc = struct.unpack('<H', msg[4+payload_len:4+payload_len+2])[0]
            if crc != self._calc_crc(payload):
                return
            try:
                response = json.loads(payload.decode('utf-8'))
                self._handle_response(response)
            except json.JSONDecodeError as je:
                self.logger.error(f"Device {self.device_id} JSON decode error: {str(je)}")
            except Exception as e:
                self.logger.error(f"Device {self.device_id} message processing error: {str(e)}")
    
    def _writer_loop(self, eventtime):
        """Цикл отправки запросов."""
        if not self._connected:
            return eventtime + 0.05
        now = eventtime
        if now - self._last_status_request > (0.2 if self._park_in_progress else 1.0):
            self._request_status()
            self._last_status_request = now
        if not self._queue.empty():
            task = self._queue.get_nowait()
            if task:
                request, callback = task
                self._callback_map[request['id']] = callback
                if not self._send_request(request):
                    self.logger.warning(f"Device {self.device_id} failed to send request, requeuing...")
                    self._queue.put(task)
        return eventtime + 0.05
    
    def _request_status(self):
        """Запросить статус устройства."""
        def status_callback(response):
            if 'result' in response:
                self._info.update(response['result'])
        if self.reactor.monotonic() - self._last_status_request > (0.2 if self._park_in_progress else 1.0):
            try:
                self.send_request({
                    "id": self._get_next_request_id(),
                    "method": "get_status"
                }, status_callback)
                self._last_status_request = self.reactor.monotonic()
            except Exception as e:
                self.logger.error(f"Device {self.device_id} status request error: {str(e)}")
    
    def _handle_response(self, response: dict):
        """Обработка ответа от устройства."""
        if 'id' in response:
            callback = self._callback_map.pop(response['id'], None)
            if callback:
                try:
                    callback(response)
                except Exception as e:
                    self.logger.error(f"Device {self.device_id} callback error: {str(e)}")
        if 'result' in response and isinstance(response['result'], dict):
            result = response['result']
            
            # Нормализация данных о сушилке
            if 'dryer_status' in result and isinstance(result['dryer_status'], dict):
                result['dryer'] = result['dryer_status']
            self._info.update(result)
            
            # Обработка парковки
            if self._park_in_progress:
                self._handle_parking_response(result)
    
    def _handle_parking_response(self, result: dict):
        """Обработка ответа во время парковки."""
        current_status = result.get('status', 'unknown')
        current_assist_count = result.get('feed_assist_count', 0)
        elapsed_time = self.reactor.monotonic() - self._park_start_time
        
        self.logger.debug(f"Device {self.device_id} parking check: slot {self._park_index}, " +
                         f"count={current_assist_count}, last={self._last_assist_count}, " +
                         f"hits={self._assist_hit_count}, elapsed={elapsed_time:.1f}s")
        
        if current_status == 'ready':
            if current_assist_count != self._last_assist_count:
                self._last_assist_count = current_assist_count
                self._assist_hit_count = 0
                if current_assist_count > 0:
                    self._park_count_increased = True
                    self.logger.info(f"Device {self.device_id} feed assist working for slot {self._park_index}, count: {current_assist_count}")
            else:
                self._assist_hit_count += 1
                
                # Check if feed assist is actually working
                if elapsed_time > 3.0 and not self._park_count_increased:
                    self.logger.error(f"Device {self.device_id} feed assist for slot {self._park_index} not working")
                    self._park_error = True
                    self._park_in_progress = False
                    self._park_index = -1
                    return
                
                # Проверка завершения парковки будет в ValgAce с использованием park_hit_count
    
    def complete_parking(self, park_hit_count: int, disable_assist_after_toolchange: bool):
        """Завершить парковку."""
        if not self._park_in_progress:
            return
        self.logger.info(f"Device {self.device_id} parking completed for slot {self._park_index}")
        try:
            self.send_request({
                "method": "stop_feed_assist",
                "params": {"index": self._park_index}
            }, lambda r: None)
        except Exception as e:
            self.logger.error(f"Device {self.device_id} parking completion error: {str(e)}")
        finally:
            self._park_in_progress = False
            self._park_error = False
            self._park_index = -1
            if disable_assist_after_toolchange:
                self._feed_assist_index = -1
    
    # =========================================================================
    # Utility Methods
    # =========================================================================
    
    def dwell(self, delay: float = 1.0, callback: Optional[Callable] = None):
        """Асинхронная пауза через reactor."""
        if delay <= 0:
            if callback:
                try:
                    callback()
                except Exception as e:
                    self.logger.error(f"Device {self.device_id} error in dwell callback: {e}")
            return
        
        def timer_handler(event_time):
            if callback:
                try:
                    callback()
                except Exception as e:
                    self.logger.error(f"Device {self.device_id} error in dwell callback: {e}")
            return self.reactor.NEVER
        
        self.reactor.register_timer(timer_handler, self.reactor.monotonic() + delay)
    
    def get_slot_status(self, local_slot: int) -> Dict:
        """
        Получить статус локального слота (0-3).
        
        Args:
            local_slot: Индекс слота на устройстве (0-3)
        Returns:
            Статус слота или None если индекс неверный
        """
        if 0 <= local_slot < 4:
            return self._info['slots'][local_slot]
        return None
    
    def get_status(self) -> Dict:
        """Получить полное состояние устройства."""
        return self._info.copy()
    
    def is_connected(self) -> bool:
        """Проверить, подключено ли устройство."""
        return self._connected
    
    def check_parking_progress(self, park_hit_count: int) -> Tuple[bool, bool, bool]:
        """
        Проверить прогресс парковки.
        
        Args:
            park_hit_count: Количество проверок для завершения
        Returns:
            Tuple[completed, error, in_progress]
        """
        if not self._park_in_progress:
            return (False, self._park_error, False)
        
        if self._park_error:
            return (False, True, False)
        
        if self._assist_hit_count >= park_hit_count and self._park_count_increased:
            return (True, False, False)
        
        # Schedule dwell if not already scheduled
        if not self._dwell_scheduled:
            self._dwell_scheduled = True
            self.dwell(0.7, lambda: setattr(self, '_dwell_scheduled', False))
        
        return (False, False, True)


# =============================================================================
# ValgAce - Manager for multiple ACE devices
# =============================================================================

class ValgAce:
    """
    Модуль ValgAce для Klipper
    Обеспечивает управление массивом устройств автоматической смены филамента (ACE)
    Поддерживает до 4 устройств (16 слотов) с возможностью сушки, подачи и обратной подачи филамента
    
    Multi-device Architecture:
    - ValgAce является менеджером для массива ACEDevice
    - Глобальные слоты 0-15 отображаются на (device_id, local_slot)
    - device_id = global_slot // 4
    - local_slot = global_slot % 4
    """
    def __init__(self, config):
        self.printer = config.get_printer()
        self.toolhead = None
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        
        # Initialize logger first
        self.logger = logging.getLogger('ace')
        self._name = 'ace'
        
        # === MULTI-DEVICE CONFIGURATION ===
        self.max_devices = config.getint('max_devices', 4)
        self.devices: Dict[int, ACEDevice] = {}
        
        # Initialize devices from config
        for device_id in range(self.max_devices):
            # Для device_id=0 проверяем 'serial', для остальных 'serial_N'
            if device_id == 0:
                serial_port = config.get('serial', None)
            else:
                serial_port = config.get(f'serial_{device_id}', None)
            
            if serial_port:
                self.devices[device_id] = ACEDevice(
                    device_id=device_id,
                    config=config,
                    reactor=self.reactor,
                    parent_logger=self.logger,
                    gcode=self.gcode
                )
                self.logger.info(f"Configured ACE device {device_id} at {serial_port}")
        
        # === GLOBAL SLOT MAPPING ===
        self._build_slot_mapping()
        
        # === COMMON CONFIGURATION ===
        self.feed_speed = config.getint('feed_speed', 50)
        self.retract_speed = config.getint('retract_speed', 50)
        self.retract_mode = config.getint('retract_mode', 0)
        self.toolchange_retract_length = config.getint('toolchange_retract_length', 100)
        self.park_hit_count = config.getint('park_hit_count', 5)
        self.max_dryer_temperature = config.getint('max_dryer_temperature', 55)
        self.disable_assist_after_toolchange = config.getboolean('disable_assist_after_toolchange', True)
        self.infinity_spool_mode = config.getboolean('infinity_spool_mode', False)
        
        # Aggressive parking parameters
        self.aggressive_parking = config.getboolean('aggressive_parking', False)
        self.max_parking_distance = config.getint('max_parking_distance', 100)
        self.parking_speed = config.getint('parking_speed', 10)
        
        # === FILAMENT SENSOR ===
        self.filament_sensor_name = config.get('filament_sensor', None)
        self.filament_sensor = None
        if self.filament_sensor_name:
            try:
                self.filament_sensor = self.printer.lookup_object(f'filament_switch_sensor {self.filament_sensor_name}')
                self.logger.info(f"Filament sensor '{self.filament_sensor_name}' found and connected")
            except Exception as e:
                self.logger.warning(f"Filament sensor '{self.filament_sensor_name}' not found: {str(e)}")
                self.filament_sensor = None
        
        # === VARIABLES ===
        try:
            save_vars = self.printer.lookup_object('save_variables')
            self.variables = save_vars.allVariables
        except self.printer.config_error:
            self.variables = {}
            self.logger.warning("save_variables module not found, variables will not persist across restarts")
        
        # === CURRENT TOOL TRACKING ===
        # Глобальный индекс текущего инструмента (-1 = не выбран)
        self._current_global_slot = -1
        
        # === TOOLCHANGE STATE ===
        self._park_is_toolchange = False
        self._park_previous_tool = -1
        self._post_toolchange_running = False
        
        # === CONNECTION CHECK TIMER ===
        self._connect_check_timer = self.reactor.register_timer(self._connect_check, self.reactor.NOW)
        
        # Register events and commands
        self._register_handlers()
        self._register_gcode_commands()
    
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
        self.logger.info(f"Slot mapping built: {self._total_slots} total slots across {len(self.devices)} devices")
    
    def get_device_and_slot(self, global_slot: int) -> Tuple[Optional[ACEDevice], int]:
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
        
        Args:
            device_id: ID устройства (0-3)
            local_slot: Локальный слот (0-3)
        Returns:
            Глобальный индекс слота или -1 если не найден
        """
        return self._device_slot_to_global.get((device_id, local_slot), -1)
    
    def global_to_local(self, global_slot: int) -> Tuple[int, int]:
        """
        Конвертировать глобальный слот в (device_id, local_slot).
        
        Args:
            global_slot: Глобальный индекс (0-15)
        Returns:
            Tuple[device_id, local_slot]
        """
        if global_slot < 0 or global_slot >= self._total_slots:
            raise ValueError(f"Invalid global slot: {global_slot}")
        return global_slot // 4, global_slot % 4
    
    def local_to_global(self, device_id: int, local_slot: int) -> int:
        """
        Конвертировать (device_id, local_slot) в глобальный слот.
        
        Args:
            device_id: ID устройства (0-3)
            local_slot: Локальный слот (0-3)
        Returns:
            Глобальный индекс слота
        """
        if device_id not in self.devices:
            raise ValueError(f"Invalid device_id: {device_id}")
        if local_slot < 0 or local_slot > 3:
            raise ValueError(f"Invalid local_slot: {local_slot}")
        return device_id * 4 + local_slot
    
    def is_single_device_mode(self) -> bool:
        """Проверить, работает ли модуль в режиме одного устройства."""
        return len(self.devices) == 1
    
    def _register_handlers(self):
        """Регистрация обработчиков событий принтера."""
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect', self._handle_disconnect)
    
    def _register_gcode_commands(self):
        """Регистрация G-code команд."""
        commands = [
            ('ACE_DEBUG', self.cmd_ACE_DEBUG, "Debug connection"),
            ('ACE_STATUS', self.cmd_ACE_STATUS, "Get device status"),
            ('ACE_START_DRYING', self.cmd_ACE_START_DRYING, "Start drying"),
            ('ACE_STOP_DRYING', self.cmd_ACE_STOP_DRYING, "Stop drying"),
            ('ACE_ENABLE_FEED_ASSIST', self.cmd_ACE_ENABLE_FEED_ASSIST, "Enable feed assist"),
            ('ACE_DISABLE_FEED_ASSIST', self.cmd_ACE_DISABLE_FEED_ASSIST, "Disable feed assist"),
            ('ACE_PARK_TO_TOOLHEAD', self.cmd_ACE_PARK_TO_TOOLHEAD, "Park filament to toolhead"),
            ('ACE_FEED', self.cmd_ACE_FEED, "Feed filament"),
            ('ACE_UPDATE_FEEDING_SPEED', self.cmd_ACE_UPDATE_FEEDING_SPEED, "Update feeding speed"),
            ('ACE_STOP_FEED', self.cmd_ACE_STOP_FEED, "Stop feed filament"),
            ('ACE_RETRACT', self.cmd_ACE_RETRACT, "Retract filament"),
            ('ACE_UPDATE_RETRACT_SPEED', self.cmd_ACE_UPDATE_RETRACT_SPEED, "Update retracting speed"),
            ('ACE_STOP_RETRACT', self.cmd_ACE_STOP_RETRACT, "Stop retract filament"),
            ('ACE_CHANGE_TOOL', self.cmd_ACE_CHANGE_TOOL, "Change tool"),
            ('ACE_INFINITY_SPOOL', self.cmd_ACE_INFINITY_SPOOL, "Change tool when current spool is empty"),
            ('ACE_SET_INFINITY_SPOOL_ORDER', self.cmd_ACE_SET_INFINITY_SPOOL_ORDER, "Set infinity spool slot order"),
            ('ACE_FILAMENT_INFO', self.cmd_ACE_FILAMENT_INFO, "Show filament info"),
            ('ACE_CHECK_FILAMENT_SENSOR', self.cmd_ACE_CHECK_FILAMENT_SENSOR, "Check filament sensor status"),
            ('ACE_DISCONNECT', self.cmd_ACE_DISCONNECT, "Force disconnect device"),
            ('ACE_CONNECT', self.cmd_ACE_CONNECT, "Connect to device"),
            ('ACE_CONNECTION_STATUS', self.cmd_ACE_CONNECTION_STATUS, "Check connection status"),
            # Multi-device commands
            ('ACE_LIST_DEVICES', self.cmd_ACE_LIST_DEVICES, "List all configured devices"),
            ('ACE_DEVICE_STATUS', self.cmd_ACE_DEVICE_STATUS, "Get specific device status"),
        ]
        for name, func, desc in commands:
            self.gcode.register_command(name, func, desc=desc)
    
    def _connect_check(self, eventtime):
        """Периодическая проверка подключения устройств."""
        for device_id, device in self.devices.items():
            if not device.is_connected() and not device._manually_disconnected:
                device.connect()
        return eventtime + 1.0
    
    def _handle_ready(self):
        """Обработчик события готовности принтера."""
        self.toolhead = self.printer.lookup_object('toolhead')
        if self.toolhead is None:
            raise self.printer.config_error("Toolhead not found in ValgAce module")
    
    def _handle_disconnect(self):
        """Обработчик события отключения принтера."""
        for device in self.devices.values():
            device._manually_disconnected = False
            device.disconnect()
    
    def _save_variable(self, name: str, value):
        """Safely save variable if save_variables module is available."""
        self.variables[name] = value
        try:
            self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE={name} VALUE={value}')
        except Exception as e:
            self.logger.debug(f"Could not save variable {name}: {e}")
    
    # =========================================================================
    # Status API for Moonraker
    # =========================================================================
    
    def get_status(self, eventtime):
        """
        Возвращает статус для Moonraker API через query_objects.
        Поддерживает как одиночное устройство (обратная совместимость),
        так и массив устройств.
        """
        # Single device mode (backward compatible)
        if self.is_single_device_mode():
            device = self.devices[0]
            return self._get_single_device_status(device, eventtime)
        
        # Multi-device mode
        devices_status = {}
        all_slots = []
        
        for device_id, device in self.devices.items():
            device_status = self._get_single_device_status(device, eventtime)
            devices_status[device_id] = device_status
            
            # Add device_id and global_index to each slot
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
            'slots': all_slots,  # Flat list of all slots
            'filament_sensor': self._get_filament_sensor_status(eventtime)
        }
    
    def _get_single_device_status(self, device: ACEDevice, eventtime) -> Dict:
        """Получить статус одного устройства (для обратной совместимости)."""
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
            'boot_firmware': info.get('boot_firmware', ''),
            'temp': info.get('temp', 0),
            'fan_speed': info.get('fan_speed', 0),
            'enable_rfid': info.get('enable_rfid', 0),
            'feed_assist_count': info.get('feed_assist_count', 0),
            'cont_assist_time': info.get('cont_assist_time', 0.0),
            'feed_assist_slot': device._feed_assist_index,
            'dryer': dryer_normalized,
            'dryer_status': dryer_normalized,
            'slots': info.get('slots', []),
            'connected': device._connected,
            'device_id': device.device_id
        }
    
    def _get_filament_sensor_status(self, eventtime) -> Optional[Dict]:
        """Получить статус датчика филамента."""
        if self.filament_sensor:
            try:
                return self.filament_sensor.get_status(eventtime)
            except Exception as e:
                self.logger.warning(f"Error getting filament sensor status: {str(e)}")
                return {"filament_detected": False, "enabled": False}
        return None
    
    # =========================================================================
    # G-code Commands
    # =========================================================================
    
    def cmd_ACE_LIST_DEVICES(self, gcmd):
        """Вывести список всех настроенных устройств."""
        output = ["=== ACE Devices ==="]
        output.append(f"Total devices: {len(self.devices)}")
        output.append(f"Total slots: {self._total_slots}")
        output.append(f"Mode: {'single' if self.is_single_device_mode() else 'multi_device'}")
        output.append("")
        
        for device_id, device in sorted(self.devices.items()):
            status = "connected" if device._connected else "disconnected"
            output.append(f"Device {device_id}:")
            output.append(f"  Serial: {device.serial_name}")
            output.append(f"  Status: {status}")
            if device._connected:
                output.append(f"  Model: {device._info.get('model', 'Unknown')}")
                output.append(f"  Firmware: {device._info.get('firmware', 'Unknown')}")
            output.append(f"  Global slots: {device_id * 4} - {device_id * 4 + 3}")
            output.append("")
        
        gcmd.respond_info("\n".join(output))
    
    def cmd_ACE_DEVICE_STATUS(self, gcmd):
        """Получить статус конкретного устройства."""
        device_id = gcmd.get_int('DEVICE', 0, minval=0, maxval=self.max_devices - 1)
        
        if device_id not in self.devices:
            gcmd.respond_raw(f"Error: Device {device_id} not configured")
            return
        
        device = self.devices[device_id]
        
        def status_callback(response):
            if 'result' in response:
                result = response['result']
                if 'dryer_status' in result and isinstance(result['dryer_status'], dict):
                    result['dryer'] = result['dryer_status']
                device._info.update(result)
                self._output_device_status(gcmd, device)
        
        device.send_request({"method": "get_status"}, status_callback)
    
    def _output_device_status(self, gcmd, device: ACEDevice):
        """Вывод статуса конкретного устройства."""
        info = device._info
        output = []
        
        output.append(f"=== ACE Device {device.device_id} Status ===")
        output.append(f"Status: {info.get('status', 'unknown')}")
        output.append(f"Connected: {'Yes' if device._connected else 'No'}")
        
        if 'model' in info:
            output.append(f"Model: {info.get('model', 'Unknown')}")
        if 'firmware' in info:
            output.append(f"Firmware: {info.get('firmware', 'Unknown')}")
        
        output.append("")
        
        # Dryer Status
        output.append("=== Dryer ===")
        dryer = info.get('dryer', {})
        if not dryer and 'dryer_status' in info:
            dryer = info.get('dryer_status', {})
        
        dryer_status = dryer.get('status', 'unknown') if isinstance(dryer, dict) else 'unknown'
        output.append(f"Status: {dryer_status}")
        if dryer_status == 'drying':
            output.append(f"Target Temperature: {dryer.get('target_temp', 0)}°C")
            output.append(f"Current Temperature: {info.get('temp', 0)}°C")
            duration = dryer.get('duration', 0)
            output.append(f"Duration: {duration} minutes")
            remain_time_raw = dryer.get('remain_time', 0)
            remain_time = remain_time_raw / 60 if remain_time_raw > 0 else 0
            if remain_time > 0:
                total_minutes = int(remain_time)
                fractional_part = remain_time - total_minutes
                seconds = int(round(fractional_part * 60))
                if seconds >= 60:
                    total_minutes += 1
                    seconds = 0
                if total_minutes > 0:
                    output.append(f"Remaining Time: {total_minutes}m {seconds}s" if seconds > 0 else f"Remaining Time: {total_minutes}m")
        else:
            output.append(f"Temperature: {info.get('temp', 0)}°C")
        
        output.append("")
        
        # Slots
        output.append("=== Filament Slots ===")
        for slot in info.get('slots', []):
            index = slot.get('index', -1)
            global_index = self.get_global_slot(device.device_id, index)
            status = slot.get('status', 'unknown')
            slot_type = slot.get('type', '')
            
            output.append(f"Slot {index} (Global {global_index}):")
            output.append(f"  Status: {status}")
            if slot_type:
                output.append(f"  Type: {slot_type}")
        
        gcmd.respond_info("\n".join(output))
    
    def cmd_ACE_STATUS(self, gcmd):
        """Получить статус всех устройств."""
        try:
            # Для single device mode - обратная совместимость
            if self.is_single_device_mode():
                device = self.devices[0]
                
                def status_callback(response):
                    if 'result' in response:
                        result = response['result']
                        if 'dryer_status' in result and isinstance(result['dryer_status'], dict):
                            result['dryer'] = result['dryer_status']
                        device._info.update(result)
                        self._output_status(gcmd)
                
                device.send_request({"method": "get_status"}, status_callback)
            else:
                # Multi-device mode - выводим статус всех устройств
                self._output_all_devices_status(gcmd)
                
        except Exception as e:
            self.logger.error(f"Status command error: {str(e)}")
            gcmd.respond_raw(f"Error retrieving status: {str(e)}")
    
    def _output_status(self, gcmd):
        """Вывод статуса ACE (вызывается после получения данных) - single device mode."""
        try:
            device = self.devices[0]
            info = device._info
            output = []
            
            output.append("=== ACE Device Status ===")
            output.append(f"Status: {info.get('status', 'unknown')}")
            
            if 'model' in info:
                output.append(f"Model: {info.get('model', 'Unknown')}")
            if 'firmware' in info:
                output.append(f"Firmware: {info.get('firmware', 'Unknown')}")
            if 'boot_firmware' in info:
                output.append(f"Boot Firmware: {info.get('boot_firmware', 'Unknown')}")
            
            output.append("")
            
            # Dryer Status
            output.append("=== Dryer ===")
            dryer = info.get('dryer', {})
            if not dryer and 'dryer_status' in info:
                dryer = info.get('dryer_status', {})
            
            dryer_status = dryer.get('status', 'unknown') if isinstance(dryer, dict) else 'unknown'
            output.append(f"Status: {dryer_status}")
            if dryer_status == 'drying':
                output.append(f"Target Temperature: {dryer.get('target_temp', 0)}°C")
                output.append(f"Current Temperature: {info.get('temp', 0)}°C")
                duration = dryer.get('duration', 0)
                output.append(f"Duration: {duration} minutes")
                remain_time_raw = dryer.get('remain_time', 0)
                remain_time = remain_time_raw / 60 if remain_time_raw > 0 else 0
                if remain_time > 0:
                    total_minutes = int(remain_time)
                    fractional_part = remain_time - total_minutes
                    seconds = int(round(fractional_part * 60))
                    if seconds >= 60:
                        total_minutes += 1
                        seconds = 0
                    if total_minutes > 0:
                        if seconds > 0:
                            output.append(f"Remaining Time: {total_minutes}m {seconds}s")
                        else:
                            output.append(f"Remaining Time: {total_minutes}m")
                    else:
                        output.append(f"Remaining Time: {seconds}s")
            else:
                output.append(f"Temperature: {info.get('temp', 0)}°C")
            
            output.append("")
            
            # Device Parameters
            output.append("=== Device Parameters ===")
            output.append(f"Fan Speed: {info.get('fan_speed', 0)} RPM")
            output.append(f"RFID Enabled: {'Yes' if info.get('enable_rfid', 0) else 'No'}")
            output.append(f"Feed Assist Count: {info.get('feed_assist_count', 0)}")
            cont_assist = info.get('cont_assist_time', 0.0)
            if cont_assist > 0:
                output.append(f"Continuous Assist Time: {cont_assist:.1f} ms")
            
            output.append("")
            
            # Slots Information
            output.append("=== Filament Slots ===")
            slots = info.get('slots', [])
            for slot in slots:
                index = slot.get('index', -1)
                status = slot.get('status', 'unknown')
                slot_type = slot.get('type', '')
                color = slot.get('color', [0, 0, 0])
                sku = slot.get('sku', '')
                rfid_status = slot.get('rfid', 0)
                
                output.append(f"Slot {index}:")
                output.append(f"  Status: {status}")
                if slot_type:
                    output.append(f"  Type: {slot_type}")
                if sku:
                    output.append(f"  SKU: {sku}")
                if color and isinstance(color, list) and len(color) >= 3:
                    output.append(f"  Color: RGB({color[0]}, {color[1]}, {color[2]})")
                rfid_text = {0: "Not found", 1: "Failed", 2: "Identified", 3: "Identifying"}.get(rfid_status, "Unknown")
                output.append(f"  RFID: {rfid_text}")
                output.append("")
            
            # Filament Sensor Status
            if self.filament_sensor:
                try:
                    eventtime = self.reactor.monotonic()
                    sensor_status = self.filament_sensor.get_status(eventtime)
                    
                    filament_detected = sensor_status.get('filament_detected', False)
                    sensor_enabled = sensor_status.get('enabled', False)
                    
                    output.append("=== Filament Sensor ===")
                    if filament_detected:
                        output.append("Status: filament detected")
                    else:
                        output.append("Status: filament not detected")
                    output.append(f"Enabled: {'Yes' if sensor_enabled else 'No'}")
                    output.append("")
                except Exception as e:
                    output.append("=== Filament Sensor ===")
                    output.append(f"Error reading sensor: {str(e)}")
                    output.append("")
            
            gcmd.respond_info("\n".join(output))
        except Exception as e:
            self.logger.error(f"Status output error: {str(e)}")
            gcmd.respond_raw(f"Error outputting status: {str(e)}")
    
    def _output_all_devices_status(self, gcmd):
        """Вывод статуса всех устройств (multi-device mode)."""
        output = ["=== ACE Multi-Device Status ==="]
        output.append(f"Total devices: {len(self.devices)}")
        output.append(f"Total slots: {self._total_slots}")
        output.append(f"Current tool: {self._current_global_slot}")
        output.append("")
        
        for device_id, device in sorted(self.devices.items()):
            status = "connected" if device._connected else "disconnected"
            output.append(f"Device {device_id} ({device.serial_name}): {status}")
            
            if device._connected:
                info = device._info
                output.append(f"  Model: {info.get('model', 'Unknown')}")
                output.append(f"  Firmware: {info.get('firmware', 'Unknown')}")
                output.append(f"  Temperature: {info.get('temp', 0)}°C")
                
                # Slots
                for slot in info.get('slots', []):
                    local_index = slot.get('index', -1)
                    global_index = self.get_global_slot(device_id, local_index)
                    slot_status = slot.get('status', 'unknown')
                    slot_type = slot.get('type', '')
                    type_str = f" ({slot_type})" if slot_type else ""
                    output.append(f"    Slot {local_index} [T{global_index}]: {slot_status}{type_str}")
            output.append("")
        
        gcmd.respond_info("\n".join(output))
    
    def cmd_ACE_DEBUG(self, gcmd):
        """Debug command for testing."""
        method = gcmd.get('METHOD')
        params = gcmd.get('PARAMS', '{}')
        device_id = gcmd.get_int('DEVICE', 0, minval=0, maxval=self.max_devices - 1)
        
        if device_id not in self.devices:
            gcmd.respond_raw(f"Error: Device {device_id} not configured")
            return
        
        device = self.devices[device_id]
        
        try:
            request = {"method": method}
            if params.strip():
                request["params"] = json.loads(params)
            
            def callback(response):
                gcmd.respond_info(json.dumps(response, indent=2))
            
            device.send_request(request, callback)
        except Exception as e:
            self.logger.error(f"Debug command error: {str(e)}")
            gcmd.respond_raw(f"Error: {str(e)}")
    
    def cmd_ACE_FILAMENT_INFO(self, gcmd):
        """Получить информацию о филаменте в слоте."""
        # Поддерживаем глобальный индекс (0-15) или локальный (0-3) для обратной совместимости
        index = gcmd.get_int('INDEX', minval=0, maxval=self._total_slots - 1)
        
        device, local_slot = self.get_device_and_slot(index)
        if not device:
            gcmd.respond_raw(f"Error: Invalid slot {index}")
            return
        
        try:
            def callback(response):
                if 'result' in response:
                    slot_info = response['result']
                    slot_info['global_index'] = index
                    slot_info['device_id'] = device.device_id
                    self.gcode.respond_info(str(slot_info))
                else:
                    self.gcode.respond_info('Error: No result in response')
            
            device.send_request({"method": "get_filament_info", "params": {"index": local_slot}}, callback)
        except Exception as e:
            self.logger.error(f"Filament info error: {str(e)}")
            self.gcode.respond_info('Error: ' + str(e))
    
    def cmd_ACE_CHECK_FILAMENT_SENSOR(self, gcmd):
        """Command to check the filament sensor status."""
        if self.filament_sensor:
            try:
                eventtime = self.reactor.monotonic()
                sensor_status = self.filament_sensor.get_status(eventtime)
                
                filament_detected = sensor_status.get('filament_detected', False)
                sensor_enabled = sensor_status.get('enabled', False)
                
                if filament_detected:
                    gcmd.respond_info("Filament sensor: filament detected")
                else:
                    gcmd.respond_info("Filament sensor: filament not detected")
                    
                gcmd.respond_info(f"Filament sensor: {'enabled' if sensor_enabled else 'disabled'}")
            except Exception as e:
                gcmd.respond_info(f"Error checking filament sensor: {str(e)}")
        else:
            gcmd.respond_info("No filament sensor configured")
    
    def cmd_ACE_START_DRYING(self, gcmd):
        """Запустить сушку на устройстве."""
        temperature = gcmd.get_int('TEMP', minval=20, maxval=self.max_dryer_temperature)
        duration = gcmd.get_int('DURATION', 240, minval=1)
        device_id = gcmd.get_int('DEVICE', 0, minval=0, maxval=self.max_devices - 1)
        
        if device_id not in self.devices:
            gcmd.respond_raw(f"Error: Device {device_id} not configured")
            return
        
        device = self.devices[device_id]
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                gcmd.respond_info(f"Drying started on device {device_id} at {temperature}°C for {duration} minutes")
        
        device.send_request({
            "method": "drying",
            "params": {
                "temp": temperature,
                "fan_speed": 7000,
                "duration": duration
            }
        }, callback)
    
    def cmd_ACE_STOP_DRYING(self, gcmd):
        """Остановить сушку на устройстве."""
        device_id = gcmd.get_int('DEVICE', 0, minval=0, maxval=self.max_devices - 1)
        
        if device_id not in self.devices:
            gcmd.respond_raw(f"Error: Device {device_id} not configured")
            return
        
        device = self.devices[device_id]
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                gcmd.respond_info(f"Drying stopped on device {device_id}")
        
        device.send_request({"method": "drying_stop"}, callback)
    
    def cmd_ACE_ENABLE_FEED_ASSIST(self, gcmd):
        """Включить вспомогательную подачу для слота."""
        # Поддерживаем глобальный индекс (0-15)
        index = gcmd.get_int('INDEX', minval=0, maxval=self._total_slots - 1)
        
        device, local_slot = self.get_device_and_slot(index)
        if not device:
            gcmd.respond_raw(f"Error: Invalid slot {index}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                device._feed_assist_index = local_slot
                gcmd.respond_info(f"Feed assist enabled for slot {index} (device {device.device_id}, local {local_slot})")
                device.dwell(0.3, lambda: None)
        
        device.send_request({"method": "start_feed_assist", "params": {"index": local_slot}}, callback)
    
    def cmd_ACE_DISABLE_FEED_ASSIST(self, gcmd):
        """Выключить вспомогательную подачу."""
        # Поддерживаем глобальный индекс (0-15)
        index = gcmd.get_int('INDEX', minval=0, maxval=self._total_slots - 1)
        
        device, local_slot = self.get_device_and_slot(index)
        if not device:
            gcmd.respond_raw(f"Error: Invalid slot {index}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                device._feed_assist_index = -1
                gcmd.respond_info(f"Feed assist disabled for slot {index}")
                device.dwell(0.3, lambda: None)
        
        device.send_request({"method": "stop_feed_assist", "params": {"index": local_slot}}, callback)
    
    def cmd_ACE_PARK_TO_TOOLHEAD(self, gcmd):
        """Парковка филамента к соплу."""
        # Проверяем, есть ли парковка в процессе на любом устройстве
        for device in self.devices.values():
            if device._park_in_progress:
                gcmd.respond_raw("Already parking to toolhead")
                return
        
        # Поддерживаем глобальный индекс (0-15)
        index = gcmd.get_int('INDEX', minval=0, maxval=self._total_slots - 1)
        
        device, local_slot = self.get_device_and_slot(index)
        if not device:
            gcmd.respond_raw(f"Error: Invalid slot {index}")
            return
        
        if device.get_slot_status(local_slot).get('status') != 'ready':
            self.gcode.run_script_from_command(f"_ACE_ON_EMPTY_ERROR INDEX={index}")
            return
        
        self._park_to_toolhead(device, local_slot, index)
    
    def cmd_ACE_FEED(self, gcmd):
        """Подача филамента."""
        index = gcmd.get_int('INDEX', minval=0, maxval=self._total_slots - 1)
        length = gcmd.get_int('LENGTH', minval=1)
        speed = gcmd.get_int('SPEED', self.feed_speed, minval=1)
        
        device, local_slot = self.get_device_and_slot(index)
        if not device:
            gcmd.respond_raw(f"Error: Invalid slot {index}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        
        device.send_request({
            "method": "feed_filament",
            "params": {"index": local_slot, "length": length, "speed": speed}
        }, callback)
        device.dwell((length / speed) + 0.1, lambda: None)
    
    def cmd_ACE_UPDATE_FEEDING_SPEED(self, gcmd):
        """Обновить скорость подачи."""
        index = gcmd.get_int('INDEX', minval=0, maxval=self._total_slots - 1)
        speed = gcmd.get_int('SPEED', self.feed_speed, minval=1)
        
        device, local_slot = self.get_device_and_slot(index)
        if not device:
            gcmd.respond_raw(f"Error: Invalid slot {index}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        
        device.send_request({
            "method": "update_feeding_speed",
            "params": {"index": local_slot, "speed": speed}
        }, callback)
        device.dwell(0.5, lambda: None)
    
    def cmd_ACE_STOP_FEED(self, gcmd):
        """Остановить подачу."""
        index = gcmd.get_int('INDEX', minval=0, maxval=self._total_slots - 1)
        
        device, local_slot = self.get_device_and_slot(index)
        if not device:
            gcmd.respond_raw(f"Error: Invalid slot {index}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                gcmd.respond_info("Feed stopped")
        
        device.send_request({
            "method": "stop_feed_filament",
            "params": {"index": local_slot},
        }, callback)
        device.dwell(0.5, lambda: None)
    
    def cmd_ACE_RETRACT(self, gcmd):
        """Втягивание филамента."""
        index = gcmd.get_int('INDEX', minval=0, maxval=self._total_slots - 1)
        length = gcmd.get_int('LENGTH', minval=1)
        speed = gcmd.get_int('SPEED', self.retract_speed, minval=1)
        mode = gcmd.get_int('MODE', self.retract_mode, minval=0, maxval=1)
        
        device, local_slot = self.get_device_and_slot(index)
        if not device:
            gcmd.respond_raw(f"Error: Invalid slot {index}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        
        device.send_request({
            "method": "unwind_filament",
            "params": {"index": local_slot, "length": length, "speed": speed, "mode": mode}
        }, callback)
        device.dwell((length / speed) + 0.1, lambda: None)
    
    def cmd_ACE_UPDATE_RETRACT_SPEED(self, gcmd):
        """Обновить скорость втягивания."""
        index = gcmd.get_int('INDEX', minval=0, maxval=self._total_slots - 1)
        speed = gcmd.get_int('SPEED', self.retract_speed, minval=1)
        
        device, local_slot = self.get_device_and_slot(index)
        if not device:
            gcmd.respond_raw(f"Error: Invalid slot {index}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        
        device.send_request({
            "method": "update_unwinding_speed",
            "params": {"index": local_slot, "speed": speed}
        }, callback)
        device.dwell(0.5, lambda: None)
    
    def cmd_ACE_STOP_RETRACT(self, gcmd):
        """Остановить втягивание."""
        index = gcmd.get_int('INDEX', minval=0, maxval=self._total_slots - 1)
        
        device, local_slot = self.get_device_and_slot(index)
        if not device:
            gcmd.respond_raw(f"Error: Invalid slot {index}")
            return
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                gcmd.respond_info("Retract stopped")
        
        device.send_request({
            "method": "stop_unwind_filament",
            "params": {"index": local_slot},
        }, callback)
        device.dwell(0.5, lambda: None)
    
    # =========================================================================
    # Parking Implementation
    # =========================================================================
    
    def _park_to_toolhead(self, device: ACEDevice, local_slot: int, global_slot: int):
        """
        Парковка филамента к соплу.
        
        Args:
            device: Устройство ACE
            local_slot: Локальный слот (0-3)
            global_slot: Глобальный слот (для логирования)
        """
        # Check if aggressive parking should be used
        if self.aggressive_parking:
            self.logger.info(f"Using aggressive parking method for global slot {global_slot}")
            self._sensor_based_parking(device, local_slot, global_slot)
        else:
            # Set parking flag BEFORE sending request
            device._park_in_progress = True
            device._park_error = False
            device._park_index = local_slot
            device._assist_hit_count = 0
            device._park_start_time = self.reactor.monotonic()
            device._park_count_increased = False
            
            self.logger.info(f"Starting traditional parking for global slot {global_slot} (device {device.device_id}, local {local_slot})")
            
            def callback(response):
                if response.get('code', 0) != 0:
                    self.logger.error(f"ACE Error starting feed assist: {response.get('msg', 'Unknown error')}")
                    device._park_in_progress = False
                    self.logger.error(f"Parking aborted for global slot {global_slot}")
                else:
                    device._last_assist_count = response.get('result', {}).get('feed_assist_count', 0)
                    self.logger.info(f"Feed assist started for global slot {global_slot}, count: {device._last_assist_count}")
                device.dwell(0.3, lambda: None)
            
            device.send_request({"method": "start_feed_assist", "params": {"index": local_slot}}, callback)
    
    def _sensor_based_parking(self, device: ACEDevice, local_slot: int, global_slot: int):
        """
        Alternative parking algorithm using filament sensor detection.
        """
        if not self.filament_sensor:
            self.logger.error("Filament sensor not configured for sensor-based parking")
            return False
        
        self.logger.info(f"Starting sensor-based parking for global slot {global_slot}")
        
        # Set parking flags
        device._park_in_progress = True
        device._park_error = False
        device._park_index = local_slot
        device._park_start_time = self.reactor.monotonic()
        
        # Calculate timeout
        timeout_duration = (self.max_parking_distance / self.parking_speed) + 10
        self.logger.info(f"Sensor-based parking timeout: {timeout_duration:.1f}s")
        
        def start_feed_callback(response):
            if response.get('code', 0) != 0:
                self.logger.error(f"Error starting feed for sensor-based parking: {response.get('msg', 'Unknown error')}")
                device._park_in_progress = False
                device._park_error = True
                return
            
            self.logger.info(f"Started feeding filament for global slot {global_slot}")
            self._monitor_filament_sensor_for_parking(device, local_slot, global_slot, timeout_duration)
        
        device.send_request({
            "method": "feed_filament",
            "params": {"index": local_slot, "length": self.max_parking_distance, "speed": self.parking_speed}
        }, start_feed_callback)
        
        return True
    
    def _monitor_filament_sensor_for_parking(self, device: ACEDevice, local_slot: int, global_slot: int, timeout_duration: float):
        """Monitor the filament sensor during parking."""
        start_time = self.reactor.monotonic()
        
        def check_sensor(eventtime):
            if not device._park_in_progress:
                return self.reactor.NEVER
            
            elapsed = eventtime - start_time
            if elapsed > timeout_duration:
                self.logger.error(f"Sensor-based parking timeout for global slot {global_slot}")
                device.send_request({"method": "stop_feed_filament", "params": {"index": local_slot}}, lambda r: None)
                device._park_in_progress = False
                device._park_error = True
                return self.reactor.NEVER
            
            try:
                sensor_status = self.filament_sensor.get_status(eventtime)
                filament_detected = sensor_status.get('filament_detected', False)
                
                if filament_detected:
                    self.logger.info(f"Filament detected for global slot {global_slot}")
                    device.send_request({"method": "stop_feed_filament", "params": {"index": local_slot}}, lambda r: None)
                    self._switch_to_traditional_parking(device, local_slot, global_slot)
                    return self.reactor.NEVER
                else:
                    return eventtime + 0.1
            except Exception as e:
                self.logger.error(f"Error checking filament sensor: {str(e)}")
                device.send_request({"method": "stop_feed_filament", "params": {"index": local_slot}}, lambda r: None)
                device._park_in_progress = False
                device._park_error = True
                return self.reactor.NEVER
        
        self.reactor.register_timer(check_sensor, self.reactor.NOW)
    
    def _switch_to_traditional_parking(self, device: ACEDevice, local_slot: int, global_slot: int):
        """Switch from sensor-based parking to traditional parking algorithm."""
        self.logger.info(f"Completing parking after sensor detection for global slot {global_slot}")
        
        def enable_assist_callback(response):
            if response.get('code', 0) != 0:
                self.logger.error(f"Error enabling feed assist: {response.get('msg', 'Unknown error')}")
                device._park_error = True
            else:
                device._feed_assist_index = local_slot
                self.logger.info(f"Feed assist enabled for global slot {global_slot}")
            
            device.complete_parking(self.park_hit_count, self.disable_assist_after_toolchange)
        
        device.send_request({"method": "start_feed_assist", "params": {"index": local_slot}}, enable_assist_callback)
    
    # =========================================================================
    # Tool Change
    # =========================================================================
    
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
        else:
            device, local_slot = None, -1
        
        # Получаем устройство для предыдущего инструмента
        if was != -1:
            prev_device, prev_local_slot = self.get_device_and_slot(was)
        else:
            prev_device, prev_local_slot = None, -1
        
        # Выполняем pre-toolchange макрос
        self.gcode.run_script_from_command(f"_ACE_PRE_TOOLCHANGE FROM={was} TO={tool}")
        self._park_is_toolchange = True
        self._park_previous_tool = was
        
        if self.toolhead:
            self.toolhead.wait_moves()
        
        # Обновляем текущий инструмент
        self._current_global_slot = tool
        self._save_variable('ace_current_index', tool)
        
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        
        # === RETRACT PREVIOUS TOOL ===
        if prev_device and prev_local_slot >= 0:
            prev_device.send_request({
                "method": "unwind_filament",
                "params": {
                    "index": prev_local_slot,
                    "length": self.toolchange_retract_length,
                    "speed": self.retract_speed
                }
            }, callback)
            
            # Wait for retract to physically complete
            retract_time = (self.toolchange_retract_length / self.retract_speed) + 1.0
            self.logger.info(f"Waiting {retract_time:.1f}s for retract to complete")
            if self.toolhead:
                self.toolhead.dwell(retract_time)
            
            # Wait for slot to be ready
            self.logger.info(f"Waiting for slot {was} to be ready")
            timeout = self.reactor.monotonic() + 10.0
            while prev_device._info['slots'][prev_local_slot]['status'] != 'ready':
                if self.reactor.monotonic() > timeout:
                    gcmd.respond_raw(f"ACE Error: Timeout waiting for slot {was} to be ready")
                    return
                if self.toolhead:
                    self.toolhead.dwell(1.0)
            
            self.logger.info(f"Slot {was} is ready, parking new tool {tool}")
            
            # === PARK NEW TOOL ===
            if tool != -1:
                self._park_to_toolhead(device, local_slot, tool)
                
                # Wait for parking to complete
                self.logger.info(f"Waiting for parking to complete (slot {tool})")
                timeout = self.reactor.monotonic() + 60.0
                while device._park_in_progress:
                    if device._park_error:
                        gcmd.respond_raw(f"ACE Error: Parking failed for slot {tool}")
                        return
                    
                    # Check parking progress
                    completed, error, in_progress = device.check_parking_progress(self.park_hit_count)
                    if completed:
                        device.complete_parking(self.park_hit_count, self.disable_assist_after_toolchange)
                        break
                    if error:
                        gcmd.respond_raw(f"ACE Error: Parking failed for slot {tool}")
                        return
                    
                    if self.reactor.monotonic() > timeout:
                        gcmd.respond_raw(f"ACE Error: Timeout waiting for parking to complete")
                        return
                    if self.toolhead:
                        self.toolhead.dwell(1.0)
                
                self.logger.info(f"Parking completed, executing post-toolchange")
                if self.toolhead:
                    self.toolhead.wait_moves()
                
                # Execute post-toolchange macro
                self.gcode.run_script_from_command(f'_ACE_POST_TOOLCHANGE FROM={was} TO={tool}')
                if self.toolhead:
                    self.toolhead.wait_moves()
                gcmd.respond_info(f"Tool changed from {was} to {tool}")
            else:
                # Unloading only, no new tool
                self.gcode.run_script_from_command(f'_ACE_POST_TOOLCHANGE FROM={was} TO={tool}')
                if self.toolhead:
                    self.toolhead.wait_moves()
                gcmd.respond_info(f"Tool changed from {was} to {tool}")
        else:
            # No previous tool, just park the new one
            if tool != -1:
                self.logger.info(f"Starting parking for slot {tool} (no previous tool)")
                self._park_to_toolhead(device, local_slot, tool)
                
                # Wait for parking to complete
                self.logger.info(f"Waiting for parking to complete (slot {tool})")
                timeout = self.reactor.monotonic() + 60.0
                while device._park_in_progress:
                    if device._park_error:
                        gcmd.respond_raw(f"ACE Error: Parking failed for slot {tool}")
                        return
                    
                    completed, error, in_progress = device.check_parking_progress(self.park_hit_count)
                    if completed:
                        device.complete_parking(self.park_hit_count, self.disable_assist_after_toolchange)
                        break
                    if error:
                        gcmd.respond_raw(f"ACE Error: Parking failed for slot {tool}")
                        return
                    
                    if self.reactor.monotonic() > timeout:
                        gcmd.respond_raw(f"ACE Error: Timeout waiting for parking to complete")
                        return
                    if self.toolhead:
                        self.toolhead.dwell(1.0)
                
                self.logger.info(f"Parking completed, executing post-toolchange")
                if self.toolhead:
                    self.toolhead.wait_moves()
                
                self.gcode.run_script_from_command(f'_ACE_POST_TOOLCHANGE FROM={was} TO={tool}')
                if self.toolhead:
                    self.toolhead.wait_moves()
                gcmd.respond_info(f"Tool changed from {was} to {tool}")
    
    # =========================================================================
    # Infinity Spool
    # =========================================================================
    
    def cmd_ACE_SET_INFINITY_SPOOL_ORDER(self, gcmd):
        """Set the order of slots for infinity spool mode."""
        order_str = gcmd.get('ORDER', '')
        
        if not order_str:
            gcmd.respond_raw("Error: ORDER parameter is required")
            gcmd.respond_info("Usage: ACE_SET_INFINITY_SPOOL_ORDER ORDER=\"0,1,2,3\"")
            gcmd.respond_info(f"Note: Use global slot indices (0-{self._total_slots - 1})")
            return
        
        # Parse order string
        try:
            order_list = [item.strip().lower() for item in order_str.split(',')]
            
            # Validate order - can have up to total_slots items
            if len(order_list) > self._total_slots:
                gcmd.respond_raw(f"Error: Order can contain at most {self._total_slots} items, got {len(order_list)}")
                return
            
            # Validate each item
            valid_slots = []
            for i, item in enumerate(order_list):
                if item == 'none':
                    valid_slots.append('none')
                else:
                    try:
                        slot_num = int(item)
                        if slot_num < 0 or slot_num >= self._total_slots:
                            gcmd.respond_raw(f"Error: Slot number {slot_num} at position {i+1} is out of range (0-{self._total_slots - 1})")
                            return
                        valid_slots.append(slot_num)
                    except ValueError:
                        gcmd.respond_raw(f"Error: Invalid value '{item}' at position {i+1}. Use slot number or 'none'")
                        return
            
            # Save order
            order_str_saved = ','.join(str(s) if s != 'none' else 'none' for s in valid_slots)
            self._save_variable('ace_infsp_order', order_str_saved)
            self._save_variable('ace_infsp_position', 0)
            
            gcmd.respond_info(f"Infinity spool order set: {order_str_saved}")
            
        except Exception as e:
            self.logger.error(f"Error setting infinity spool order: {str(e)}")
            gcmd.respond_raw(f"Error: {str(e)}")
    
    def cmd_ACE_INFINITY_SPOOL(self, gcmd):
        """Change tool when current spool is empty (infinity spool mode)."""
        was = self._current_global_slot
        infsp_status = self.infinity_spool_mode
        
        if not infsp_status:
            gcmd.respond_info("ACE_INFINITY_SPOOL disabled")
            return
        if was == -1:
            gcmd.respond_info("Tool is not set")
            return
        
        # Get order from variables
        order_str = self.variables.get('ace_infsp_order', '')
        if not order_str:
            gcmd.respond_raw("Error: Infinity spool order not set. Use ACE_SET_INFINITY_SPOOL_ORDER first")
            return
        
        # Parse order
        try:
            order_list = []
            for item in order_str.split(','):
                item = item.strip().lower()
                if item == 'none':
                    order_list.append('none')
                else:
                    order_list.append(int(item))
        except Exception as e:
            self.logger.error(f"Error parsing infinity spool order: {str(e)}")
            gcmd.respond_raw(f"Error: Invalid order format: {order_str}")
            return
        
        # Find current position in order
        saved_position = self.variables.get('ace_infsp_position', -1)
        current_order_index = -1
        
        if saved_position >= 0 and saved_position < len(order_list):
            if order_list[saved_position] != 'none' and order_list[saved_position] == was:
                current_order_index = saved_position
            else:
                for i, slot in enumerate(order_list):
                    if slot != 'none' and slot == was:
                        current_order_index = i
                        break
        else:
            for i, slot in enumerate(order_list):
                if slot != 'none' and slot == was:
                    current_order_index = i
                    break
        
        if current_order_index == -1:
            self.logger.warning(f"Current slot {was} not found in order, starting from beginning")
            current_order_index = -1
        
        # Find next valid slot
        tool = None
        new_position = None
        
        for i in range(len(order_list)):
            next_index = (current_order_index + 1 + i) % len(order_list)
            next_slot = order_list[next_index]
            
            if next_slot == 'none':
                continue
            
            # Check if slot is ready
            device, local_slot = self.get_device_and_slot(next_slot)
            if device and device._connected and device.get_slot_status(local_slot).get('status') == 'ready':
                tool = next_slot
                new_position = next_index
                break
        
        if tool is None:
            gcmd.respond_raw("Error: No more ready slots available in order")
            return
        
        # Check if new slot is ready
        device, local_slot = self.get_device_and_slot(tool)
        if not device or not device._connected or device.get_slot_status(local_slot).get('status') != 'ready':
            gcmd.respond_raw(f"ACE Error: Slot {tool} is not ready")
            return
        
        self.logger.info(f"INFINITY_SPOOL: changing from {was} to {tool}")
        
        # Pre-processing
        self.gcode.run_script_from_command("_ACE_PRE_INFINITYSPOOL")
        if self.toolhead:
            self.toolhead.wait_moves()
        
        # Track parking success
        parking_success = {'completed': False}
        
        def on_park_complete():
            if parking_success['completed']:
                return
            parking_success['completed'] = True
            
            self.logger.info(f"INFINITY_SPOOL: parking complete for slot {tool}")
            self.gcode.run_script_from_command('_ACE_POST_INFINITYSPOOL')
            if self.toolhead:
                self.toolhead.wait_moves()
            
            self._save_variable('ace_current_index', tool)
            self._save_variable('ace_infsp_position', new_position)
            gcmd.respond_info(f"Tool changed from {was} to {tool}")
        
        def on_park_error():
            if parking_success['completed']:
                return
            parking_success['completed'] = True
            
            self.logger.error(f"INFINITY_SPOOL: parking failed for slot {tool}")
            gcmd.respond_raw(f"ACE Error: Failed to park slot {tool}")
        
        # Start parking
        self._park_to_toolhead(device, local_slot, tool)
        if self.toolhead:
            self.toolhead.wait_moves()
        
        # Monitor parking with timeout
        max_wait_time = 30.0
        start_time = self.reactor.monotonic()
        
        def check_parking_status(eventtime):
            elapsed = eventtime - start_time
            
            if device._park_error:
                on_park_error()
                return self.reactor.NEVER
            
            completed, error, in_progress = device.check_parking_progress(self.park_hit_count)
            if completed:
                device.complete_parking(self.park_hit_count, self.disable_assist_after_toolchange)
                on_park_complete()
                return self.reactor.NEVER
            
            if error or not device._park_in_progress:
                on_park_error()
                return self.reactor.NEVER
            
            if elapsed > max_wait_time:
                self.logger.error(f"INFINITY_SPOOL: parking timeout after {elapsed:.1f}s")
                device._park_in_progress = False
                device._park_error = True
                on_park_error()
                return self.reactor.NEVER
            
            return eventtime + 0.5
        
        self.reactor.register_timer(check_parking_status, self.reactor.monotonic() + 0.5)
    
    # =========================================================================
    # Connection Management Commands
    # =========================================================================
    
    def cmd_ACE_DISCONNECT(self, gcmd):
        """G-code command to force disconnect from device(s)."""
        device_id = gcmd.get_int('DEVICE', -1, minval=-1, maxval=self.max_devices - 1)
        
        try:
            if device_id == -1:
                # Disconnect all devices
                for device in self.devices.values():
                    if device._connected:
                        device._manually_disconnected = True
                        device.disconnect()
                gcmd.respond_info("All ACE devices disconnected successfully")
            else:
                if device_id not in self.devices:
                    gcmd.respond_raw(f"Error: Device {device_id} not configured")
                    return
                device = self.devices[device_id]
                if device._connected:
                    device._manually_disconnected = True
                    device.disconnect()
                    gcmd.respond_info(f"ACE device {device_id} disconnected successfully")
                else:
                    gcmd.respond_info(f"ACE device {device_id} is already disconnected")
        except Exception as e:
            self.logger.error(f"Error during forced disconnect: {str(e)}")
            gcmd.respond_raw(f"Error disconnecting: {str(e)}")
    
    def cmd_ACE_CONNECT(self, gcmd):
        """G-code command to connect to device(s)."""
        device_id = gcmd.get_int('DEVICE', -1, minval=-1, maxval=self.max_devices - 1)
        
        try:
            if device_id == -1:
                # Connect all devices
                for device in self.devices.values():
                    if not device._connected:
                        device._manually_disconnected = False
                        device.connect()
                gcmd.respond_info("Connection attempt for all devices")
            else:
                if device_id not in self.devices:
                    gcmd.respond_raw(f"Error: Device {device_id} not configured")
                    return
                device = self.devices[device_id]
                if device._connected:
                    gcmd.respond_info(f"ACE device {device_id} is already connected")
                else:
                    device._manually_disconnected = False
                    success = device.connect()
                    if success:
                        gcmd.respond_info(f"ACE device {device_id} connected successfully")
                    else:
                        gcmd.respond_raw(f"Failed to connect to ACE device {device_id}")
        except Exception as e:
            self.logger.error(f"Error during manual connect: {str(e)}")
            gcmd.respond_raw(f"Error connecting: {str(e)}")
    
    def cmd_ACE_CONNECTION_STATUS(self, gcmd):
        """G-code command to check connection status."""
        device_id = gcmd.get_int('DEVICE', -1, minval=-1, maxval=self.max_devices - 1)
        
        try:
            if device_id == -1:
                # Show all devices status
                output = ["=== ACE Connection Status ==="]
                for dev_id, device in sorted(self.devices.items()):
                    status = "connected" if device._connected else "disconnected"
                    output.append(f"Device {dev_id}: {status}")
                    if device._connected:
                        output.append(f"  Model: {device._info.get('model', 'Unknown')}")
                        output.append(f"  Firmware: {device._info.get('firmware', 'Unknown')}")
                    output.append(f"  Serial: {device.serial_name}")
                gcmd.respond_info("\n".join(output))
            else:
                if device_id not in self.devices:
                    gcmd.respond_raw(f"Error: Device {device_id} not configured")
                    return
                device = self.devices[device_id]
                status = "connected" if device._connected else "disconnected"
                gcmd.respond_info(f"ACE Device {device_id} Connection Status: {status}")
                if device._connected:
                    gcmd.respond_info(f"Model: {device._info.get('model', 'Unknown')}")
                    gcmd.respond_info(f"Firmware: {device._info.get('firmware', 'Unknown')}")
                gcmd.respond_info(f"Serial: {device.serial_name}")
        except Exception as e:
            self.logger.error(f"Error checking connection status: {str(e)}")
            gcmd.respond_raw(f"Error checking status: {str(e)}")


def load_config(config):
    """Load the ValgAce module."""
    return ValgAce(config)
