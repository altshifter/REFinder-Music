import telebot
import yt_dlp
import os
import re
import logging
import json
import sqlite3
from datetime import datetime
import time
from telebot import types


####НАСТРОЙКИ БОТА
ADMIN_USER_ID = '123456789'  #ID администратора для рассылки сообщений
DATABASE_FILE = 'telemusic.db'  #путь к файлу базы данных
CACHE_DB_FILE = 'telegram_bot_cache.json'  #путь к файлу кэша
DOWNLOAD_DIR = "/path/to/dir/"  #папка для загруженных файлов
CACHE_LIFETIME = 60*60*24*30*6  #время жизни кэша результатов поиска (в секундах, здесь 6 месяцев)
FILE_ROTATE = 50  #максимальное количество хранимых аудиофайлов
MAX_LONG = 720  #максимальная длительность загрузки музыки в секундах (12 минут)(при прямой ссылке)
RESULTS_PAGES = 5  #количество результатов, отображаемых на странице.
MAX_TITLE_LENGTH = 40  #максимально допустимая длина заголовка перед усечением или сжатием
DROP_VOWELS = 50  #процент гласных, которые нужно сжать в заголовке.
MAX_RESULTS = 15  #количество возвращаемых результатов поиска («ytsearch10» возвращает первые 10)..
MAX_DURATION = 900  #максимальная продолжительность видео в секундах (15 минут)(при поиске)

bot = telebot.TeleBot('KEY')# Токен Telegram бота
logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s', level=logging.DEBUG)
requests = {}
cached_search_results = {}
downloaded_files_cache = {}


##БД
# Подключаемся к базе данных 
conn = sqlite3.connect(DATABASE_FILE)
cursor = conn.cursor()
# Создаем таблицу пользователей
cursor.execute('''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    status TEXT DEFAULT NULL
)
''')

# Создаем таблицу истории запросов
cursor.execute('''
CREATE TABLE IF NOT EXISTS search_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    keywords TEXT NOT NULL,
    last_call TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, keywords),
    FOREIGN KEY(user_id) REFERENCES users(id)
)
''')

# Создаем таблицу истории загрузок
cursor.execute('''
CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT NOT NULL,
    video_url TEXT NOT NULL,
    filename TEXT NOT NULL,
    UNIQUE(user_id, video_url),
    FOREIGN KEY(user_id) REFERENCES users(id)
)
''')

conn.commit()  # Сохраняем изменения
conn.close()   # Закрываем соединение с базой данных


def db_connect():
    return sqlite3.connect(DATABASE_FILE) 
    
#добавляем пользователя в базу данных
def db_add_user(user_id, username, status=None):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO users (id, username, status) VALUES (?, ?, ?)', (user_id, username, status))
    conn.commit()
    conn.close()
    
#добавляем запись в историю поиска
def db_add_search(user_id, username, keywords):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO search_history (user_id, username, keywords, last_call)
    VALUES (?, ?, ?, CURRENT_TIMESTAMP)
    ON CONFLICT(user_id, keywords) DO UPDATE SET last_call = excluded.last_call
    ''', (user_id, username, keywords))
    conn.commit()
    conn.close()
    
#добавляем запись в историю загрузок
def db_add_download(user_id, username, video_url, filename):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO download_history (user_id, username, video_url, filename)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(user_id, video_url) DO UPDATE SET filename = excluded.filename
    ''', (user_id, username, video_url, filename))
    conn.commit()
    conn.close()

#объявления, chat_id из базы данных
def load_chat_ids():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM users')
    chat_ids = [row[0] for row in cursor.fetchall()]
    conn.close()
    return chat_ids


##КЕШ ПОИСКА
#для подгрузки найденого
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

# Получаем результаты из кэша, если они не устарели
def get_cached_search_results(keywords):
    cur_time = time.time()
    cached = cached_search_results.get(keywords, None)
    if cached and cur_time - cached['timestamp'] < CACHE_LIFETIME:  # Проверяем возраст кэша
        return cached['results']
    else:
        return None
        
# Сохраняем результаты и временную метку в кэш
def cache_search_results(keywords, results):
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
    
# Сохраняем путь к файлу и URL в кэш
def cache_file_path(url, file_path, download_url):
    downloaded_files_cache[url] = {
        'file_path': file_path,
        'download_url': download_url
    }
    save_cache()  # Сохраняем кэш при каждом его обновлении
    
    
##ФАЙЛЫ
#следим за скаченными файлами
def rotate_files(directory, max_files=FILE_ROTATE):
    files = [file for file in os.listdir(directory) if file.endswith('.mp3')]
    while len(files) > max_files:
        oldest_file = min(files, key=lambda x: os.path.getctime(os.path.join(directory, x)))
        os.remove(os.path.join(directory, oldest_file))
        files.remove(oldest_file)
        logging.info(f'Удален старый файл: {oldest_file}')

#сохраняем
def safe_filename(filename):
    safe_filename = re.sub(r'[\\/*?:"<>|]', "", filename)
    safe_filename = re.sub(r'\s+', " ", safe_filename).strip()
    return safe_filename


##РАБОТА С ТЕКСТОМ
# Функция для проверки запроса на URL YT
def is_youtube_url(url):
    youtube_regex = (
        r'(https?://)?(www\.)?'
        '(youtube|youtu|youtube-nocookie)\.(com|be)/'
        '(watch\?v=|embed/|v/|.+\?v=)?([^&=%\?]{11})')
    youtube_match = re.match(youtube_regex, url)
    return bool(youtube_match)
   
#Будем ломать слова для Саши(сокращение текста в кнопках)
def compress_title(title, vowel_pct=DROP_VOWELS):
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

def trim_or_compress_title(title, max_length=MAX_TITLE_LENGTH):
    if len(title) <= max_length:
        return title
    return compress_title(title)[:max_length-3] + '...'
    
#наверное можно сразу из либы доставать, но потом доку почитаю
def format_duration(seconds):
    minutes, sec = divmod(seconds, 60)
    return f"{minutes:02d}:{sec:02d}"
    
#изолируем
def sanitize(input_string):
    return re.sub(r'[^\w\s,.!?-]', '', input_string)
    
    
##ПОИСК И ЗАГРУЗКА МУЗЫКИ
#поиск трека
def search_music(username, keywords, search_amount=MAX_RESULTS):
    logging.info(f'Начинаем поиск музыки по ключевым словам: {keywords}')
    cached_results = get_cached_search_results(keywords)
    if cached_results is not None:
        return cached_results
    ydl_opts = {
        'default_search': f'ytsearch{search_amount}',
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
    
#загрузка и конвертация трека
def download_and_convert_music(user_id, url):
    logging.info(f'def download_and_convert_music {url}')
    rotate_files(DOWNLOAD_DIR)
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
        temp_file_path = ydl.prepare_filename(info) # Вытаскиваем заголовок и исполнителя для формирования итогового имени файла
        title = safe_filename(info.get('title', 'Unknown Title'))
        safe_mp3_filename = f'{title}.mp3'
        temp_file_path = temp_file_path.removesuffix('.webm').removesuffix('.m4a').removesuffix('.mp4') + '.mp3' # расширение файла на основе postprocessor, уже должно быть mp3
        new_file_path = os.path.join(os.path.dirname(temp_file_path), safe_mp3_filename) # Полный путь к файлу, куда будет переименован файл
        if os.path.exists(temp_file_path):  # Проверка, существует ли скачанный файл по временному пути
            os.rename(temp_file_path, new_file_path)  # Переименование оригинального файла в новое имя файла
            cache_file_path(url, new_file_path, url)
            logging.info(f'Файл успешно сохранен как {new_file_path}')
        else:
            logging.error(f'Скачанный файл {temp_file_path} не найден.')
            return None
        return new_file_path


###БОТ И ДЕЙСТВИЯ
def send_welcome(chat_id):
    welcome_text = 'Привет! Для поиска музыки введите название трека.'
    bot.send_message(chat_id, welcome_text)
    
#старт:
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    username = message.from_user.username
    chat_id = message.chat.id
    keywords = message.text
    db_add_user(user_id, username)
    # Приветственное сообщение для пользователя при активации бота командой /start
    send_welcome(chat_id)

##OVERDRIVE
#объявления
@bot.message_handler(commands=['broadcast'])
def handle_broadcast_command(message):
    if str(message.from_user.id) == ADMIN_USER_ID:
        args = message.text.split(maxsplit=1)  # Разбиваем сообщение на части
        if len(args) > 1:
            # Вторая часть - это текст сообщения, который нужно отправить
            text = args[1]
            broadcast_message(text)
            bot.reply_to(message, "Сообщение отправлено всем пользователям.")
        else:
            bot.reply_to(message, "Пожалуйста, укажите текст сообщения после команды.")
    else:
        bot.reply_to(message, "У вас нет прав использовать эту команду.")

def broadcast_message(text):
    chat_ids = load_chat_ids()
    for chat_id in chat_ids:
        try:
            bot.send_message(chat_id, text)
        except Exception as e:
            logging.error(f"Не удалось отправить сообщение пользователю {chat_id}: {e}")
#    
#запрос
@bot.message_handler(content_types=['text'])
def text(message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.username 
    keywords = sanitize(message.text)
    db_add_search(user_id, username, keywords)
    # Проверям, является ли текст сообщения ссылкой на YouTube
    if is_youtube_url(keywords):
        # Проверяем длительность видео перед скачиванием
        ydl_opts = {
            'format': 'bestaudio/best',
            'quiet': True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                info = ydl.extract_info(keywords, download=False)
                video_duration = info.get('duration', 0)  # продолжительность в секундах
            except yt_dlp.utils.DownloadError:
                # Если не удалось получить информацию о видео
                bot.send_message(chat_id, "Не удалось получить информацию о видео.")
                return
        if video_duration > 720:
            # Если видео длиннее 12 минут
            bot.send_message(chat_id, "Видео длиннее 12 минут и не может быть обработано.")
        else:
            # Если видео подходит по длительности, скачиваем и конвертируем
            file_path = download_and_convert_music(username, keywords)
            if file_path:
                # Если файл успешно скачан, отправляем музыку
                send_music(chat_id, file_path)
            else:
                # Если возникла ошибка при скачивании
                bot.send_message(chat_id, "Не удалось скачать трек.")
    else:
        # Обрабатываем остальной текст как поиск
        results = search_music(username, keywords)
        if results:
            requests[chat_id] = results
            send_results_page(chat_id, results)  # Отправляем первую страницу результатов
        else:
            bot.send_message(chat_id, "К сожалению, ничего не найдено. Попробуйте другой запрос.")  
    
#отправка сообщений с результатами поиска
def send_results_page(chat_id, results, page=1, results_per_page=RESULTS_PAGES):
    total_pages = (len(results) + results_per_page - 1) // results_per_page
    page = max(1, min(page, total_pages))
    page_results = results[(page - 1) * results_per_page: page * results_per_page]
    keyboard = types.InlineKeyboardMarkup(row_width=1)
    #кнопки для каждой песни
    for i, item in enumerate(page_results, start=(page - 1) * results_per_page):
        duration = format_duration(item['duration']) if 'duration' in item else 'Unknown'
        title = trim_or_compress_title(item.get('title', 'Unknown Title'))
        button_text = f"{i + 1}. {title} [{duration}]"
        keyboard.add(types.InlineKeyboardButton(button_text, callback_data=f'download_{i + 1}')) 
    #кнопки для навигации по страницам
    navigation_buttons = []
    if page > 1:
        navigation_buttons.append(types.InlineKeyboardButton("<<", callback_data=f'page_{page-1}'))
    if page < total_pages:
        navigation_buttons.append(types.InlineKeyboardButton(">>", callback_data=f'page_{page+1}'))
    if navigation_buttons:
        keyboard.row(*navigation_buttons)
    bot.send_message(chat_id, "Выберите трек для скачивания:", reply_markup=keyboard)
    
#страницы
@bot.callback_query_handler(func=lambda call: call.data.startswith('page_'))
def query_page(call):
    # Обработчик запросов на переключение страниц с результатами поиска
    page_num = int(call.data.split('_')[1])
    chat_id = call.message.chat.id
    send_results_page(chat_id, requests[chat_id], page=page_num)

#загрузка
@bot.callback_query_handler(func=lambda call: call.data.startswith('download_'))
def callback_query(call):
    user_id = call.from_user.id  # Получаем user_id из объекта call
    chat_id = call.message.chat.id
    username = call.from_user.username
    index = int(call.data.split('_')[1]) - 1  # Получаем индекс трека
    url = requests[chat_id][index]['webpage_url']  # Получаем URL для скачивания
    file_path = download_and_convert_music(user_id, url)  # Скачиваем и конвертируем файл 
    db_add_download(user_id, username, url, file_path)
    if file_path:  # Если файл успешно скачан
        send_music(chat_id, file_path)  # Отправляем музыку пользователю
    else:  # Если возникла ошибка при скачивании
        bot.send_message(chat_id, "Не удалось скачать трек.")  # Отправляем сообщение об ошибке
        
def send_music(chat_id, filename):
    logging.info(f'Начинаем отправку музыки в чат {chat_id} по имени файла: {filename}')
    with open(filename, 'rb') as audio:
        bot.send_audio(chat_id, audio)
    logging.info(f'Отправили музыку в чат {chat_id} по имени файла: {filename}')

# Запуск polling для обработки сообщений от пользователей
bot.polling(none_stop=True, timeout=30)
