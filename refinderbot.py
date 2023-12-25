import telebot
import yt_dlp
import os
import re
import logging
import json
from datetime import datetime
import time
from telebot import types

logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.DEBUG)

bot = telebot.TeleBot('KEY')

requests = {}
page_data = {}
cached_search_results = {}
downloaded_files_cache = {}
CACHE_DB_FILE = 'telegram_bot_cache.json'
DOWNLOAD_DIR = "/path/to/file/"

def load_cache():
    try:
        with open(CACHE_DB_FILE, 'r') as f:
            data = json.load(f)
            cached_search_results.update(data.get('search_results', {}))
            downloaded_files_cache.update(data.get('files', {}))
    except FileNotFoundError:
        logging.info(f"Файл кэша {CACHE_DB_FILE} отсутствует и будет создан новый.")
    except json.JSONDecodeError as e:
        logging.error(f"Ошибка декодирования JSON из файла кэша: {e}")

def save_cache():
    with open(CACHE_DB_FILE, 'w') as f:
        files_data = {url: {'file_path': data['file_path']} for url, data in downloaded_files_cache.items()}
        
        json.dump({
            'search_results': cached_search_results,
            'files': files_data
        }, f, indent=4)


# Загрузка кэша при старте бота
load_cache()

#  истории запросов
def log_to_json(user_login, keywords):
    log_entry = {
        'timestamp': datetime.utcnow().isoformat(),  # Текущее время в формате ISO
        'user_login': user_login,
        'keywords': keywords
    }

    # Путь к файлу лога
    log_file_path = 'searchlogs.json'

    # Загружаем предыдущие данные из файла, если он существует
    try:
        with open(log_file_path, 'r', encoding='utf-8') as log_file:
            data = json.load(log_file)
    except (FileNotFoundError, json.JSONDecodeError):
        data = []

    # Добавляем новую запись в данные
    data.append(log_entry)

    # Перезаписываем файл с новыми данными
    with open(log_file_path, 'w', encoding='utf-8') as log_file:
        json.dump(data, log_file, ensure_ascii=False, indent=4)
        log_file.write('\n')  # Добавляем новую строку после записи массива
        
# Функция для проверки URL на YouTube
def is_youtube_url(url):
    youtube_regex = (
        r'(https?://)?(www\.)?'
        '(youtube|youtu|youtube-nocookie)\.(com|be)/'
        '(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})')
    
    youtube_match = re.match(youtube_regex, url)
    return bool(youtube_match)


def get_cached_search_results(keywords):
    # Получаем результаты из кэша, если они не устарели
    cur_time = time.time()
    cached = cached_search_results.get(keywords, None)
    if cached and cur_time - cached['timestamp'] < 60*60*24*30*6:  # Проверяем возраст кэша (6 месяцев)
        return cached['results']
    else:
        return None

def cache_search_results(keywords, results):
    # Сохраняем результаты и временную метку в кэш
    cached_search_results[keywords] = {
        'timestamp': time.time(),
        'results': results
    }
    save_cache()  # Сохраняем кэш при каждом его обновлении

def get_cached_file_path(url):
    cached = downloaded_files_cache.get(url, None)
    if cached:
        return cached['file_path'], url
    return None, url

def cache_file_path(url, file_path, download_url):
    # Сохраняем путь к файлу и URL в кэш
    downloaded_files_cache[url] = {
        'file_path': file_path,
        'download_url': download_url  # Сохраняем также URL
    }
    save_cache()  # Сохраняем кэш при каждом его обновлении

def rotate_files(directory, max_files=50):
    files = [file for file in os.listdir(directory) if file.endswith('.mp3')]
    while len(files) > max_files:
        oldest_file = min(files, key=lambda x: os.path.getctime(os.path.join(directory, x)))
        os.remove(os.path.join(directory, oldest_file))
        files.remove(oldest_file)
        logging.info(f'Удален старый файл: {oldest_file}')

def safe_filename(filename):
    safe_filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    safe_filename = re.sub(r'\s+', " ", safe_filename).strip()
    return safe_filename
    
    #Будем ломать слова для Саши
def compress_title(title, vowel_pct=50):
    vowels = 'aeiouAEIOUаеёиоуыэюя'# Определяем гласные буквы
    words = title.split() # Разделяем название на слова
    new_words = [] 
    
    for word in words: # Проходимся по каждому слову
        if len(word) <= 4: # Если слово короткое, оставляем как есть
            new_words.append(word)
            continue

        num_vowels_to_compress = len([c for c in word[1:-1] if c in vowels]) * vowel_pct // 100 # Подсчитываем количество гласных к удалению
        compressed_word = []
        vowels_count = 0
        compressed_word.append(word[0]) # Копируем первую и последнюю букву
        
        for char in word[1:-1]: # Удаляем гласные из середины слова
            if vowels_count < num_vowels_to_compress and char in vowels:
                vowels_count += 1
                continue
            compressed_word.append(char)
        compressed_word.append(word[-1]) # Копируем последнюю букву
        new_words.append(''.join(compressed_word)) # Возвращаем сжатое слово

    return ' '.join(new_words) # Объединяем слова обратно в строку

def trim_or_compress_title(title, max_length=40):
    if len(title) <= max_length:
        return title
    return compress_title(title)[:max_length-3] + '...'

def search_music(user_login, keywords):
    logging.info(f'Начинаем поиск музыки по ключевым словам: {keywords}')
    log_to_json(user_login, keywords)
    cached_results = get_cached_search_results(keywords)
    if cached_results is not None:
        return cached_results
    ydl_opts = {
      'default_search': 'ytsearch15',
      'noplaylist': True,
      'quiet': True
        }
    
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(keywords, download=False)
        if not info or 'entries' not in info or not info['entries']:
            logging.error('Ничего не найдено.')
            return None
        else:
            logging.info('Нашли музыку по запросу: ' + info['entries'][0]['webpage_url'])
            filtered_entries = [
        {
            'title': e['title'],
            'webpage_url': e['webpage_url'],
            'duration': e['duration']
            # Отсюда можно добавить любые другие данные, которые считаешь необходимыми
        }
        for e in info['entries'] if e.get('duration', 0) <= 900
    ]
    cache_search_results(keywords, filtered_entries)
    return filtered_entries

def download_and_convert_music(user_login, url):
    log_to_json(user_login, url)
    rotate_files(DOWNLOAD_DIR)
    logging.info(f'Начинаем скачивание музыки по URL: {url}')
    cached_file_path, cached_url = get_cached_file_path(url)
    if cached_file_path:
      url = cached_url or url  # Используем URL из кэша, если он есть
      return cached_file_path
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
        'key': 'FFmpegExtractAudio',
        'preferredcodec': 'mp3',
        'preferredquality': '256',
         }],
         'quiet': True,
         'outtmpl': '%(id)s.%(ext)s',  # Шаблон временного имени файла
      }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        logging.info(f'Музыка скачана по URL: {url}')

        # Вытаскиваем заголовок и исполнителя для формирования итогового имени файла
        temp_file_path = ydl.prepare_filename(info)
        uploader = safe_filename(info.get('uploader', 'Unknown Artist'))
        title = safe_filename(info.get('title', 'Unknown Title'))
        safe_mp3_filename = f'{uploader} - {title}.mp3'

        # Здесь происходит расширение файла на основе postprocessor, которое уже должно быть mp3
        temp_file_path = temp_file_path.removesuffix('.webm').removesuffix('.m4a').removesuffix('.mp4') + '.mp3'

        # Полный путь к файлу, куда будет переименован файл
        new_file_path = os.path.join(os.path.dirname(temp_file_path), safe_mp3_filename)

        # Проверка, существует ли скачанный файл по временному пути
        if os.path.exists(temp_file_path):
            # Переименование оригинального файла в новое имя файла
            os.rename(temp_file_path, new_file_path)
            cache_file_path(url, new_file_path, url)
            logging.info(f'Файл успешно сохранен как {new_file_path}')
        else:
            logging.error(f'Скачанный файл {temp_file_path} не найден.')
            return None

        return new_file_path

def send_music(chat_id, filename):
    logging.info(f'Начинаем отправку музыки в чат {chat_id} по имени файла: {filename}')
    with open(filename, 'rb') as audio:
        bot.send_audio(chat_id, audio)
    logging.info(f'Отправили музыку в чат {chat_id} по имени файла: {filename}')

def format_duration(seconds):
    # Конвертируем секунды в минуты и остаток секунд
    minutes, sec = divmod(seconds, 60)
    # Формируем строку "ММ:СС"
    return f"{minutes:02d}:{sec:02d}"

def send_welcome(chat_id):
    welcome_text = 'Привет! Для поиска музыки введите название или ссылку на трек.'
    user_login = message.from_user.username
    bot.send_message(chat_id, welcome_text)

@bot.message_handler(commands=['start'])
def start(message):
    # Здесь вызываем send_welcome с chat_id, полученным из сообщения
    send_welcome(message.chat.id)
    
    
# Функция для отправки сообщений с результатами поиска
def send_results_page(chat_id, results, page=1, results_per_page=5):
    total_pages = (len(results) + results_per_page - 1) // results_per_page
    page = max(1, min(page, total_pages))
    page_results = results[(page - 1) * results_per_page: page * results_per_page]
    keyboard = types.InlineKeyboardMarkup(row_width=1)

    # Формируем кнопки для каждой песни
    for i, item in enumerate(page_results, start=(page - 1) * results_per_page):
        duration = format_duration(item['duration']) if 'duration' in item else 'Unknown'
        title = trim_or_compress_title(item.get('title', 'Unknown Title'))
        button_text = f"{i + 1}. {title} [{duration}]"
        
        keyboard.add(types.InlineKeyboardButton(button_text, callback_data=f'download_{i + 1}'))
    
    # Кнопки для навигации по страницам
    navigation_buttons = []
    if page > 1:
        navigation_buttons.append(types.InlineKeyboardButton("<<", callback_data=f'page_{page-1}'))
    if page < total_pages:
        navigation_buttons.append(types.InlineKeyboardButton(">>", callback_data=f'page_{page+1}'))
    if navigation_buttons:
        keyboard.row(*navigation_buttons)
    
    bot.send_message(chat_id, "Выберите трек для скачивания:", reply_markup=keyboard)

# функции обработчиков сообщений и коллбэков:
@bot.message_handler(commands=['start'])
def start(message):
    user_login = message.from_user.username
    chat_id = message.chat.id
    user_login = message.from_user.username
    # Приветственное сообщение для пользователя при активации бота командой /start
    send_welcome(chat_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('page_'))
def query_page(call):
    # Обработчик запросов на переключение страниц с результатами поиска
    page_num = int(call.data.split('_')[1])
    chat_id = call.message.chat.id
    send_results_page(chat_id, requests[chat_id], page=page_num)
    
    #Отправка списка

@bot.message_handler(content_types=['text'])
def text(message):
    user_login = message.from_user.username
    chat_id = message.chat.id
    keywords = message.text
    log_to_json(user_login, keywords)

    # Проверям, является ли текст сообщения ссылкой на YouTube
    if is_youtube_url(keywords):
        # Скачиваем и конвертируем музыку
        file_path = download_and_convert_music(user_login, keywords)
        if file_path:
            # Если файл успешно скачан, отправляем музыку
            send_music(chat_id, file_path)
        else:
            # Если возникла ошибка при скачивании
            bot.send_message(chat_id, "Не удалось скачать трек.")
    else:
        # Иначе продолжаем выполнение обычного поиска
        results = search_music(user_login, keywords)
        if results:
            requests[chat_id] = results
            send_results_page(chat_id, results)  # Отправляем первую страницу результатов
        else:
            bot.send_message(chat_id, "К сожалению, ничего не найдено. Попробуйте другой запрос.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('download_'))
def callback_query(call):
    user_login = call.from_user.username
    chat_id = call.message.chat.id
    index = int(call.data.split('_')[1]) - 1  # Получаем индекс трека
    url = requests[chat_id][index]['webpage_url']  # Получаем URL для скачивания
    file_path = download_and_convert_music(user_login, url)  # Скачиваем и конвертируем файл

    if file_path:  # Если файл успешно скачан
        send_music(chat_id, file_path)  # Отправляем музыку пользователю
    else:  # Если возникла ошибка при скачивании
        bot.send_message(chat_id, "Не удалось скачать трек.")  # Отправляем сообщение об ошибке
# Функция для отправки музыкального файла пользователю
def send_music(chat_id, file_path):
    with open(file_path, 'rb') as audio:
        # Отправляем аудио-файл пользователю
        bot.send_audio(chat_id, audio, timeout=120)

# Функция форматирования продолжительности трека из секунд в формат ММ:СС
def format_duration(seconds):
    minutes, sec = divmod(seconds, 60)
    return f"{minutes:02d}:{sec:02d}"

# Функция для приветственного сообщения пользователю
def send_welcome(chat_id):
    welcome_text = 'Привет! Для поиска музыки введите название трека или исполнителя.'
    bot.send_message(chat_id, welcome_text)

# Запуск polling для обработки сообщений от пользователей
bot.polling(none_stop=True, timeout=30)
