from flask import Flask, request, jsonify, abort, render_template, redirect, session, url_for
import MySQLdb.cursors
import redis
import hashlib
import html
import json
import math
import os
import pathlib
import random
import re
import string
import urllib

static_folder = pathlib.Path(__file__).resolve().parent.parent / 'public'
app = Flask(__name__, static_folder = str(static_folder), static_url_path='')

app.secret_key = 'tonymoris'

_config = {
    'db_host':       os.environ.get('ISUDA_DB_HOST', 'localhost'),
    'db_port':       int(os.environ.get('ISUDA_DB_PORT', '3306')),
    'db_socket':     os.environ.get('ISUDA_DB_SOCKET', '/var/run/mysqld/mysqld.sock'),
    'db_user':       os.environ.get('ISUDA_DB_USER', 'root'),
    'db_password':   os.environ.get('ISUDA_DB_PASSWORD', ''),
    'isupam_origin': os.environ.get('ISUPAM_ORIGIN', 'http://localhost:5050'),
}

def config(key):
    if key in _config:
        return _config[key]
    else:
        raise "config value of %s undefined" % key

def redis_pool():
    if hasattr(request, 'redis'):
        return request.redis
    # FIXME ここらへん環境変数化したい
    r = redis.Redis(unix_socket_path='/var/run/redis/redis.sock')
    return r

def dbh():
    if hasattr(request, 'isuda_db'):
        return request.isuda_db
    request.isuda_db = MySQLdb.connect(**{
        'unix_socket': config('db_socket'),
        'user': config('db_user'),
        'passwd': config('db_password'),
        'db': 'isuda',
        'charset': 'utf8mb4',
        'cursorclass': MySQLdb.cursors.DictCursor,
        'autocommit': True,
    })
    cur = request.isuda_db.cursor()
    # cur.execute("SET SESSION sql_mode='TRADITIONAL,NO_AUTO_VALUE_ON_ZERO,ONLY_FULL_GROUP_BY'")
    # cur.execute('SET NAMES utf8mb4')
    return request.isuda_db

@app.teardown_request
def close_db(exception=None):
    if hasattr(request, 'isuda_db'):
        request.isuda_db.close()

@app.template_filter()
def ucfirst(str):
    return str[0].upper() + str[-len(str) + 1:]

def set_name(func):
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if "user_id" in session:
            request.user_id   = user_id = session['user_id']
            cur = dbh().cursor()
            cur.execute('SELECT name FROM user WHERE id = %s', (user_id, ))
            user = cur.fetchone()
            if user == None:
                abort(403)
            request.user_name = user['name']

        return func(*args, **kwargs)
    return wrapper

def authenticate(func):
    import functools
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not hasattr(request, 'user_id'):
            abort(403)
        return func(*args, **kwargs)

    return wrapper

@app.route('/initialize')
def get_initialize():
    cur = dbh().cursor()
    cur.execute('DELETE FROM entry WHERE id > 7101')
    cur.execute('TRUNCATE star')

    rp = redis_pool()
    result = rp.get("entry_count")
    if result == None:
        cur.execute('SELECT COUNT(1) AS count FROM entry')
        row = cur.fetchone()
        result = str(row['count'], encoding='utf-8')
        rp.set("entry_count", result)

    return jsonify(result = 'ok')

@app.route('/')
@set_name
def get_index():
    PER_PAGE = 10
    page = int(request.args.get('page', '1'))

    cur = dbh().cursor()
    cur.execute('SELECT id, keyword, description FROM entry ORDER BY updated_at DESC LIMIT %s OFFSET %s', (PER_PAGE, PER_PAGE * (page - 1),))
    entries = cur.fetchall()
    for entry in entries:
        # FIXME ここN+1です
        entry['html'] = htmlify(entry)
        entry['stars'] = load_stars(entry['keyword'])

    result = int(redis_pool().get("entry_count"))
    total_entries = result
    last_page = int(math.ceil(total_entries / PER_PAGE))
    pages = range(max(1, page - 5), min(last_page, page+5) + 1)

    return render_template('index.html', entries = entries, page = page, last_page = last_page, pages = pages)

@app.route('/robots.txt')
def get_robot_txt():
    abort(404)

@app.route('/keyword', methods=['POST'])
@set_name
@authenticate
def create_keyword():
    keyword = request.form['keyword']
    if keyword == None or len(keyword) == 0:
        abort(400)

    user_id = request.user_id
    description = request.form['description']

    if is_spam_contents(description) or is_spam_contents(keyword):
        abort(400)

    cur = dbh().cursor()
    sql = """
        INSERT INTO entry (author_id, keyword, description, created_at, updated_at)
        VALUES (%s,%s,%s,NOW(), NOW())
        ON DUPLICATE KEY UPDATE
        author_id = %s, keyword = %s, description = %s, updated_at = NOW()
"""
    cur.execute(sql, (user_id, keyword, description, user_id, keyword, description))
    cur.execute("SELECT id, description from entry where description like '%" + keyword + "%'")
    entries = cur.fetchall()

    rp = redis_pool()
    for e in entries:
        rp.delete('description_{}'.format(e['id']))
    rp.incr("entry_count")

    return redirect('/')

@app.route('/register')
@set_name
def get_register():
    return render_template('authenticate.html', action = 'register')

@app.route('/register', methods=['POST'])
def post_register():
    name = request.form['name']
    pw   = request.form['password']
    if name == None or name == '' or pw == None or pw == '':
        abort(400)

    user_id = register(dbh().cursor(), name, pw)
    session['user_id'] = user_id
    return redirect('/')

def register(cur, user, password):
    salt = random_string(20)
    cur.execute("INSERT INTO user (name, salt, password, created_at) VALUES (%s, %s, %s, NOW())", (user, salt, hashlib.sha1((salt + "password").encode('utf-8')).hexdigest(),))
    cur.execute("SELECT LAST_INSERT_ID() AS last_insert_id")
    return cur.fetchone()['last_insert_id']

def random_string(n):
    return ''.join([random.choice(string.ascii_letters + string.digits) for i in range(n)])

@app.route('/login')
@set_name
def get_login():
    return render_template('authenticate.html', action = 'login')

@app.route('/login', methods=['POST'])
def post_login():
    name = request.form['name']
    cur = dbh().cursor()
    cur.execute("SELECT id, password, salt FROM user WHERE name = %s", (name, ))
    row = cur.fetchone()
    if row == None or row['password'] != hashlib.sha1((row['salt'] + request.form['password']).encode('utf-8')).hexdigest():
        abort(403)

    session['user_id'] = row['id']
    return redirect('/')

@app.route('/logout')
def get_logout():
    session.pop('user_id', None)
    return redirect('/')

@app.route('/keyword/<keyword>')
@set_name
def get_keyword(keyword):
    if keyword == '':
        abort(400)

    cur = dbh().cursor()
    cur.execute('SELECT id, keyword, description FROM entry WHERE keyword = %s', (keyword,))
    entry = cur.fetchone()
    if entry == None:
        abort(404)

    entry['html'] = htmlify(entry)
    entry['stars'] = load_stars(entry['keyword'])
    return render_template('keyword.html', entry = entry)

@app.route('/keyword/<keyword>', methods=['POST'])
@set_name
@authenticate
def delete_keyword(keyword):
    if keyword == '':
        abort(400)

    cur = dbh().cursor()
    cur.execute('SELECT id FROM entry WHERE keyword = %s', (keyword, ))
    row = cur.fetchone()
    if row == None:
        abort(404)

    redis_pool().delete('description_{}'.format(row['id']))
    cur.execute('DELETE FROM entry WHERE keyword = %s', (keyword,))

    return redirect('/')

def htmlify(entry):
    content = entry['description']
    if content == None or content == '':
        return ''

    rp = redis_pool()
    result = rp.get("description_{}".format(entry['id']))
    if result != None:
        return str(result, encoding='utf-8')

    cur = dbh().cursor()
    cur.execute('SELECT keyword FROM entry ORDER BY CHARACTER_LENGTH(keyword) DESC')
    keywords = cur.fetchall()
    keyword_re = re.compile("(%s)" % '|'.join([ re.escape(k['keyword']) for k in keywords]))


    kw2sha = {}
    def replace_keyword(m):
        kw2sha[m.group(0)] = "isuda_%s" % hashlib.sha1(m.group(0).encode('utf-8')).hexdigest()
        return kw2sha[m.group(0)]

    result = re.sub(keyword_re, replace_keyword, content)
    result = html.escape(result)
    for kw, hash in kw2sha.items():
        url = url_for('get_keyword', keyword = kw)
        link = "<a href=\"%s\">%s</a>" % (url, html.escape(kw))
        result = re.sub(re.compile(hash), link, result)

    result = re.sub(re.compile("\n"), "<br />", result)

    redis_pool().set('description_{}'.format(entry['id']), result)
    return result

def load_stars(keyword):
    cur = dbh().cursor()
    cur.execute('SELECT user_name FROM star WHERE keyword = %s', (keyword,))
    star = cur.fetchall()
    return star

def is_spam_contents(content):
    with urllib.request.urlopen(config('isupam_origin'), urllib.parse.urlencode({ "content": content }).encode('utf-8')) as res:
        data = json.loads(res.read().decode('utf-8'))
        return not data['valid']

    return False

@app.route("/stars", methods=['POST'])
def post_stars():
    keyword = request.args.get('keyword', "")
    if keyword == None or keyword == "":
        keyword = request.form['keyword']

    cur = dbh().cursor()
    cur.execute('SELECT id FROM entry WHERE keyword = %s', (keyword,))
    entry = cur.fetchone()
    if entry == None:
        abort(404)

    user = request.args.get('user', "")
    if user == None or user == "":
        user = request.form['user']

    cur.execute('INSERT INTO star (keyword, user_name, created_at) VALUES (%s, %s, NOW())', (keyword, user))

    return jsonify(result = 'ok')

if __name__ == "__main__":
    app.run()
