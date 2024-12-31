import eventlet
eventlet.monkey_patch()

import re
import os
import hashlib
import urllib.parse
from datetime import datetime, timedelta

from flask import (
    Flask, render_template, request, send_from_directory, jsonify,
    Response, abort
)
from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO
from flask_caching import Cache
from werkzeug.utils import secure_filename
from celery import Celery

# 如果使用 redis，需要安装并启动 redis-server
import redis

#####################################
# ========== 配置区域开始 ============
#####################################

UPLOAD_DIRECTORY = "uploads"
if not os.path.exists(UPLOAD_DIRECTORY):
    os.makedirs(UPLOAD_DIRECTORY)

app = Flask(__name__)

# 数据库配置，这里使用 SQLite，实际可换成其他数据库
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///chat.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 上传相关配置
app.config['UPLOAD_FOLDER'] = UPLOAD_DIRECTORY
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # 允许最大上传 1GB

# 缓存配置，这里用 redis 做缓存
app.config['CACHE_TYPE'] = 'redis'
app.config['CACHE_REDIS_URL'] = 'redis://localhost:6379/0'

# Celery 配置，用于异步任务（可选）
app.config['CELERY_BROKER_URL'] = 'redis://localhost:6379/1'
app.config['CELERY_RESULT_BACKEND'] = 'redis://localhost:6379/2'

db = SQLAlchemy(app)
socketio = SocketIO(app, async_mode='eventlet', message_queue='redis://localhost:6379/0')
cache = Cache(app)

def make_celery(flask_app):
    celery_obj = Celery(flask_app.import_name, broker=flask_app.config['CELERY_BROKER_URL'])
    celery_obj.conf.update(flask_app.config)

    TaskBase = celery_obj.Task
    class ContextTask(TaskBase):
        def __call__(self, *args, **kwargs):
            with flask_app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)
    celery_obj.Task = ContextTask
    return celery_obj

celery = make_celery(app)

#####################################
# ========== 配置区域结束 ============
#####################################


#####################################
# ========== 数据表模型 =============
#####################################
class Message(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=True)                     # 文本内容
    filename = db.Column(db.String(255), nullable=True)          # 服务器端存储的文件名
    original_filename = db.Column(db.String(255), nullable=True) # 用户上传时的文件名
    file_size = db.Column(db.Integer, nullable=True)             # 文件大小（字节）
    # 存储为北京时间：在UTC基础上 +8 小时
    timestamp = db.Column(db.DateTime, nullable=False, default=lambda: datetime.utcnow() + timedelta(hours=8))
    is_pinned = db.Column(db.Boolean, default=False)             # 是否置顶

    def to_dict(self):
        return {
            'id': self.id,
            'text': self.text,
            'filename': self.filename,
            'original_filename': self.original_filename,
            'file_size': self.file_size,
            # 这里依旧返回字符串格式，前端用 new Date() 来解析
            'timestamp': self.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            'is_pinned': self.is_pinned
        }

#####################################
# ========== 辅助函数 ===============
#####################################

def custom_secure_filename(filename):
    """
    自定义的安全文件名函数：允许中文、英文字母、数字和 . - _
    其余字符直接去除。
    """
    filename = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5\.\-_]', '', filename)
    return filename

def get_cached_messages():
    """
    先从缓存里取消息；如无则查数据库并更新缓存
    """
    messages = cache.get('messages')
    if messages is None:
        messages = Message.query.order_by(Message.timestamp.desc()).all()
        messages = [message.to_dict() for message in messages]
        # 设置缓存 60 秒（可根据需要调整）
        cache.set('messages', messages, timeout=60)
    return messages

def clear_message_cache():
    """
    清除缓存并向所有客户端广播最新消息
    """
    cache.delete('messages')
    messages = Message.query.order_by(Message.timestamp.desc()).all()
    messages = [message.to_dict() for message in messages]
    cache.set('messages', messages, timeout=60)
    socketio.emit('update_messages', {'messages': messages}, to=None)

#####################################
# ========== 路由及逻辑 =============
#####################################

@app.route('/')
def index():
    """
    主页路由，返回聊天页面
    """
    messages = get_cached_messages()
    return render_template('index.html', messages=messages)

@app.route('/messages')
def get_messages():
    """
    返回所有消息列表（JSON），供前端第一次加载时使用
    """
    messages = get_cached_messages()
    return jsonify(messages)

@app.route('/upload', methods=['POST'])
def upload_message():
    """
    接收前端发送的文本和可选文件，并存到数据库
    同时修复“如果文件已存在，则自动重命名”的问题
    """
    text = request.form.get('text', '')
    file = request.files.get('file')
    new_filename = request.form.get('new_filename', None)

    filename = None
    original_filename = None
    file_size = None

    if file and file.filename:
        original_filename = file.filename
        # 如果前端给了新文件名，则使用新文件名，否则用原文件名
        if new_filename:
            extension = file.filename.split('.')[-1]
            base_name = custom_secure_filename(new_filename)
            filename = f"{base_name}.{extension}"
        else:
            filename = custom_secure_filename(file.filename)

        # 如果已经有同名文件，则自动在文件名后加 (1), (2), ...
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if os.path.exists(save_path):
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(save_path):
                new_name = f"{base}({counter}){ext}"
                save_path = os.path.join(app.config['UPLOAD_FOLDER'], new_name)
                counter += 1
            filename = os.path.basename(save_path)

        # 保存文件
        file.save(save_path)
        file_size = os.path.getsize(save_path)

    # 将消息信息写入数据库
    new_message = Message(
        text=text if text.strip() else None,
        filename=filename,
        original_filename=original_filename,
        file_size=file_size
    )
    db.session.add(new_message)
    db.session.commit()

    # 清缓存并广播
    clear_message_cache()

    return jsonify({'success': True})

@app.route('/toggle_pin/<int:message_id>', methods=['POST'])
def toggle_pin(message_id):
    """
    置顶/取消置顶某条消息
    """
    message = Message.query.get_or_404(message_id)
    message.is_pinned = not message.is_pinned
    db.session.commit()

    clear_message_cache()

    return jsonify({'success': True, 'is_pinned': message.is_pinned})

@app.route('/delete_message/<int:message_id>', methods=['POST'])
def delete_message(message_id):
    """
    删除某条消息，如果带文件则删除文件
    """
    message = Message.query.get_or_404(message_id)
    if message.filename:
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], message.filename)
        if os.path.exists(file_path):
            os.remove(file_path)
    db.session.delete(message)
    db.session.commit()

    clear_message_cache()
    return jsonify({'success': True})

############################################
# ========== 在线预览与下载分开 =============
############################################
@app.route('/uploads/<filename>')
def serve_file(filename):
    """
    在线预览接口，不使用 as_attachment，这样浏览器可直接预览
    （前提是浏览器支持该类型文件的在线预览，比如图片、文本、PDF 等）
    """
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.exists(file_path):
        abort(404)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=False)

@app.route('/download/<filename>')
def download_file(filename):
    """
    下载文件接口，使用 as_attachment=True，用户会直接下载
    """
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.exists(file_path):
        abort(404)
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename, as_attachment=True)

#####################################
# ========== 其他一些路由 =============
#####################################
@app.route('/ls')
def list_files():
    """
    列出 uploads 目录下所有文件
    """
    files = []
    for i, filename in enumerate(os.listdir(UPLOAD_DIRECTORY), 1):
        path = os.path.join(UPLOAD_DIRECTORY, filename)
        if os.path.isfile(path):
            try:
                files.append(f"d{i}. {filename.encode('utf-8').decode('utf-8')}")
            except UnicodeDecodeError:
                files.append(f"d{i}. {filename.encode('latin1').decode('utf-8', errors='replace')}")

    response = Response("\n".join(files), mimetype="text/plain; charset=utf-8")
    response.headers['Content-Disposition'] = 'inline'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/d<int:file_number>')
def wget_download_file(file_number):
    """
    类似命令行里 wget 的简化下载，如：/d2 表示下载列表里的第二个文件
    """
    files = [f for f in os.listdir(UPLOAD_DIRECTORY) if os.path.isfile(os.path.join(UPLOAD_DIRECTORY, f))]
    if 1 <= file_number <= len(files):
        filename = files[file_number - 1]
        file_path = os.path.join(UPLOAD_DIRECTORY, filename)
        response = send_from_directory(UPLOAD_DIRECTORY, filename, as_attachment=True)
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    else:
        return "File not found", 404

@app.route('/MD5/<filename>')
def get_md5(filename):
    """
    获取某个文件的 MD5 值
    """
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    if not os.path.exists(file_path):
        abort(404)  # 文件不存在

    with open(file_path, 'rb') as file_obj:
        file_hash = hashlib.md5()
        chunk = file_obj.read(8192)
        while chunk:
            file_hash.update(chunk)
            chunk = file_obj.read(8192)

    return file_hash.hexdigest()

#####################################
# ========== Socket.IO 事件 =========
#####################################
@socketio.on('session', namespace='/')
def handle_session(data):
    # 如果需要处理 session，可以在这里做
    pass

#####################################
# ========== 启动入口点 =============
#####################################
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        cache.clear()
    # 运行 socketio
    socketio.run(app, host='0.0.0.0', port=5001)
