import sqlite3
from flask import request, jsonify, render_template, session, redirect, url_for
from werkzeug.security import generate_password_hash

def init_admin(db):
    """Upgrades the users table and creates the hardcoded admin."""
    try:
        db.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0")
        db.execute("ALTER TABLE users ADD COLUMN is_approved INTEGER DEFAULT 0")
        db.execute("ALTER TABLE users ADD COLUMN time_spent INTEGER DEFAULT 0")
        db.commit()
    except sqlite3.OperationalError:
        pass # Columns already exist

    admin_email = "swetabh.sinha@jindalsteel.in"
    admin = db.execute("SELECT * FROM users WHERE email=?", (admin_email,)).fetchone()
    if not admin:
        db.execute(
            "INSERT INTO users (name, email, password_hash, is_admin, is_approved) VALUES (?, ?, ?, 1, 1)",
            ("Swetabh Sinha (Admin)", admin_email, generate_password_hash("CAC2025"))
        )
        db.commit()
    else:
        db.execute("UPDATE users SET is_admin=1, is_approved=1 WHERE email=?", (admin_email,))
        db.commit()

def register_admin_routes(app, get_db_func):
    @app.get('/admin')
    def admin_dashboard():
        if not session.get('is_admin'):
            return redirect(url_for('dashboard'))
        db = get_db_func()
        pending = db.execute("SELECT id, name, email, created_at FROM users WHERE is_approved=0").fetchall()
        approved = db.execute("SELECT id, name, email, time_spent, is_admin, created_at FROM users WHERE is_approved=1").fetchall()
        return render_template('admin.html', pending=[dict(p) for p in pending], approved=[dict(a) for a in approved])

    @app.post('/admin/approve/<int:user_id>')
    def approve_user(user_id):
        if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
        db = get_db_func()
        db.execute("UPDATE users SET is_approved=1 WHERE id=?", (user_id,))
        db.commit()
        return jsonify({"ok": True})

    @app.post('/admin/reject/<int:user_id>')
    def reject_user(user_id):
        if not session.get('is_admin'): return jsonify({"error": "Unauthorized"}), 403
        db = get_db_func()
        db.execute("DELETE FROM users WHERE id=?", (user_id,))
        db.commit()
        return jsonify({"ok": True})

    @app.post('/api/track_time')
    def track_time():
        user_id = session.get('user_id')
        if user_id:
            db = get_db_func()
            db.execute("UPDATE users SET time_spent = time_spent + 30 WHERE id=?", (user_id,))
            db.commit()
        return jsonify({"ok": True})