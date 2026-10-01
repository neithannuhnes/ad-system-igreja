import os
import re
import secrets
import sys
from io import BytesIO
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.sax.saxutils import escape

import psycopg
from flask import Flask, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
VENDOR_DIR = BASE_DIR / "vendor"
if VENDOR_DIR.is_dir():
    sys.path.insert(0, str(VENDOR_DIR))

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


def load_local_env():
    """Read the project's local .env without requiring another package."""
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_local_env()


def stable_secret_key():
    secret = os.environ.get("FLASK_SECRET_KEY", "")
    if len(secret) >= 32 and secret != "gere_uma_chave_local":
        return secret
    secret = secrets.token_hex(32)
    env_file = BASE_DIR / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    lines = [line for line in lines if not line.strip().startswith("FLASK_SECRET_KEY=")]
    lines.append(f"FLASK_SECRET_KEY={secret}")
    env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["FLASK_SECRET_KEY"] = secret
    return secret


app = Flask(__name__)


@app.template_filter("brl")
def format_brl(value):
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return "R$ " + f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_quantity(value):
    amount = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    return f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def report_pdf_response(title, subtitle, headers, rows, summaries, filename, width_weights,
                        filter_lines=(), extra_section=None):
    """Build a branded, in-memory PDF report and return it as a download."""
    buffer = BytesIO()
    pagesize = landscape(A4)
    doc = SimpleDocTemplate(buffer, pagesize=pagesize, leftMargin=14*mm, rightMargin=14*mm,
                            topMargin=14*mm, bottomMargin=17*mm, title=title, author="AD System")
    palette = {
        "blue": colors.HexColor("#245bc0"), "ink": colors.HexColor("#22312d"),
        "muted": colors.HexColor("#6f7d77"), "line": colors.HexColor("#dfe7e2"),
        "pale": colors.HexColor("#f3f6fb"), "white": colors.white,
    }
    styles = getSampleStyleSheet()
    brand_style = ParagraphStyle("ReportBrand", parent=styles["Normal"], fontName="Helvetica-Bold",
                                 fontSize=12, leading=14, textColor=palette["ink"])
    title_style = ParagraphStyle("ReportTitle", parent=styles["Title"], fontName="Helvetica-Bold",
                                 fontSize=18, leading=22, alignment=0, textColor=palette["ink"],
                                 spaceBefore=5, spaceAfter=4)
    meta_style = ParagraphStyle("ReportMeta", parent=styles["Normal"], fontName="Helvetica",
                                fontSize=8, leading=11, textColor=palette["muted"])
    section_style = ParagraphStyle("ReportSection", parent=styles["Heading2"], fontName="Helvetica-Bold",
                                   fontSize=11, leading=14, textColor=palette["ink"],
                                   spaceBefore=12, spaceAfter=6)
    cell_style = ParagraphStyle("ReportCell", parent=styles["BodyText"], fontName="Helvetica",
                                fontSize=7.5, leading=9.5, textColor=palette["ink"], wordWrap="CJK")
    head_style = ParagraphStyle("ReportHead", parent=cell_style, fontName="Helvetica-Bold",
                                fontSize=7, leading=8.5, textColor=palette["white"])
    stat_style = ParagraphStyle("ReportStat", parent=styles["Normal"], fontName="Helvetica",
                                fontSize=8, leading=11, textColor=palette["ink"])
    story = []
    logo_path = BASE_DIR / "static" / "logo-ad-transparente.png"
    if logo_path.exists():
        logo = Image(str(logo_path), width=13*mm, height=13*mm, kind="proportional")
        brand = Table([[logo, Paragraph("AD System", brand_style)]], colWidths=[17*mm, 55*mm])
        brand.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "MIDDLE"),
                                   ("LEFTPADDING", (0,0), (-1,-1), 0),
                                   ("RIGHTPADDING", (0,0), (-1,-1), 3),
                                   ("TOPPADDING", (0,0), (-1,-1), 0),
                                   ("BOTTOMPADDING", (0,0), (-1,-1), 0)]))
        story.append(brand)
    else:
        story.append(Paragraph("AD System", brand_style))
    story.extend([Paragraph(escape(title), title_style),
                  Paragraph(escape(subtitle), meta_style),
                  Paragraph(f"Grupo: {escape(str(session.get('nome_grupo') or 'Grupo'))}"
                            f" &nbsp;&nbsp; Gerado em: {date.today().strftime('%d/%m/%Y')}", meta_style)])
    for line in filter_lines:
        story.append(Paragraph(escape(line), meta_style))
    story.append(Spacer(1, 7*mm))

    if summaries:
        stat_cells = [Paragraph(f"<b>{escape(str(label))}</b><br/>{escape(str(value))}", stat_style)
                      for label, value in summaries]
        stats = Table([stat_cells], colWidths=[doc.width / len(stat_cells)] * len(stat_cells))
        stats.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), palette["pale"]),
            ("BOX", (0,0), (-1,-1), .5, palette["line"]),
            ("INNERGRID", (0,0), (-1,-1), .35, palette["line"]),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
            ("LEFTPADDING", (0,0), (-1,-1), 8), ("RIGHTPADDING", (0,0), (-1,-1), 8),
            ("TOPPADDING", (0,0), (-1,-1), 7), ("BOTTOMPADDING", (0,0), (-1,-1), 7),
        ]))
        story.extend([stats, Spacer(1, 5*mm)])

    def report_table(table_headers, table_rows, weights, right_columns=()):
        data = [[Paragraph(escape(str(value)), head_style) for value in table_headers]]
        for row in table_rows:
            data.append([Paragraph(escape(str(value)) if str(value) else " ", cell_style) for value in row])
        widths = [doc.width * weight / sum(weights) for weight in weights]
        table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        commands = [
            ("BACKGROUND", (0,0), (-1,0), palette["blue"]),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
            ("LINEBELOW", (0,0), (-1,0), .7, palette["blue"]),
            ("LINEBELOW", (0,1), (-1,-1), .35, palette["line"]),
            ("LEFTPADDING", (0,0), (-1,-1), 5), ("RIGHTPADDING", (0,0), (-1,-1), 5),
            ("TOPPADDING", (0,0), (-1,-1), 5), ("BOTTOMPADDING", (0,0), (-1,-1), 5),
        ]
        for row_index in range(2, len(data), 2):
            commands.append(("BACKGROUND", (0,row_index), (-1,row_index), colors.HexColor("#f8fafc")))
        for col_index in right_columns:
            commands.append(("ALIGN", (col_index,1), (col_index,-1), "RIGHT"))
            commands.append(("ALIGN", (col_index,0), (col_index,0), "RIGHT"))
        table.setStyle(TableStyle(commands))
        return table

    story.append(report_table(headers, rows or [["Nenhum registro para os filtros selecionados"] + [""] * (len(headers)-1)], width_weights,
                              right_columns=tuple(i for i, h in enumerate(headers) if h in {"Valor", "Total", "Recebido", "Pendente", "Quantidade", "Qtd.", "Idade"})))
    if extra_section:
        section_title, extra_headers, extra_rows, extra_weights, extra_right = extra_section
        story.append(Paragraph(escape(section_title), section_style))
        story.append(report_table(extra_headers, extra_rows or [["Nenhum item para somar"] + [""] * (len(extra_headers)-1)],
                                  extra_weights, right_columns=extra_right))

    def draw_page_footer(canvas, document):
        canvas.saveState()
        canvas.setStrokeColor(palette["line"])
        canvas.line(document.leftMargin, 12*mm, pagesize[0]-document.rightMargin, 12*mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(palette["muted"])
        group = str(session.get("nome_grupo") or "Grupo")[:55]
        canvas.drawString(document.leftMargin, 8*mm, f"AD System | {group}")
        canvas.drawRightString(pagesize[0]-document.rightMargin, 8*mm, f"Página {document.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_page_footer, onLaterPages=draw_page_footer)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/pdf", as_attachment=True, download_name=filename)


@app.template_filter("formatar_telefone")
def formatar_telefone(value):
    if not value:
        return "—"
    digits = "".join(char for char in str(value) if char.isdigit())
    if len(digits) == 11:
        return f"({digits[:2]}) {digits[2]} {digits[3:7]}-{digits[7:]}"
    if len(digits) == 10:
        return f"({digits[:2]}) {digits[2:6]}-{digits[6:]}"
    return str(value)
app.secret_key = stable_secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")


def current_group_id():
    return int(session["id_grupo"])


ADMIN_ENDPOINTS = {
    "admin_acessos", "admin_novo_acesso", "admin_editar_acesso", "admin_alterar_status",
    "admin_grupos", "admin_novo_grupo", "admin_renomear_grupo", "admin_excluir_grupo"
}


@app.before_request
def require_login():
    if request.endpoint in ("static", "configurar_acessos", "login"):
        return None
    if not session.get("id_usuario"):
        return redirect(url_for("login"))
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT u.ativo,COALESCE(g.nome,'Administração')
            FROM app_usuario u LEFT JOIN grupo g ON g.id_grupo=u.id_grupo
            WHERE u.id_usuario=%s""", (session["id_usuario"],))
        account = cur.fetchone()
    if not account or not account[0]:
        session.clear()
        flash("Este acesso foi desativado. Fale com o administrador do sistema.", "erro")
        return redirect(url_for("login"))
    session["nome_grupo"] = account[1]
    is_admin = bool(session.get("is_admin"))
    if is_admin and request.endpoint not in ADMIN_ENDPOINTS and request.endpoint != "sair":
        return redirect(url_for("admin_acessos"))
    if not is_admin and request.endpoint in ADMIN_ENDPOINTS:
        return redirect(url_for("inicio"))
    if not is_admin and not session.get("id_grupo"):
        session.clear()
        return redirect(url_for("login"))
    return None


@app.context_processor
def inject_group_context():
    return {"grupo_atual": session.get("nome_grupo"), "usuario_atual": session.get("usuario")}


@app.route("/configurar-acessos", methods=["GET", "POST"])
def configurar_acessos():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM app_usuario")
        if cur.fetchone()[0]:
            return redirect(url_for("login"))
    if request.method == "POST":
        username = request.form.get("usuario", "").strip()
        password = request.form.get("senha", "")
        confirm = request.form.get("confirmar_senha", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            flash("Use um usuário de 3 a 64 caracteres: letras, números, ponto, hífen ou sublinhado.", "erro")
        elif len(password) < 12:
            flash("A senha precisa ter pelo menos 12 caracteres.", "erro")
        elif password != confirm:
            flash("As senhas não coincidem.", "erro")
        else:
            try:
                with connect() as conn, conn.cursor() as cur:
                    cur.execute("INSERT INTO app_usuario (id_grupo,usuario,senha_hash,is_admin) VALUES (NULL,%s,%s,TRUE)",
                                (username, generate_password_hash(password)))
                flash("Acesso de administrador criado. Entre para configurar os grupos e líderes.", "sucesso")
                return redirect(url_for("login"))
            except psycopg.errors.UniqueViolation:
                flash("Esse usuário já está em uso.", "erro")
            except Exception:
                app.logger.exception("Falha ao criar o administrador inicial")
                flash("Não foi possível criar o acesso. Confira a conexão com o banco.", "erro")
    return render_template("configurar_acessos.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM app_usuario")
        if cur.fetchone()[0] == 0:
            return redirect(url_for("configurar_acessos"))
    if request.method == "POST":
        username = request.form.get("usuario", "").strip()
        password = request.form.get("senha", "")
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT u.id_usuario,u.id_grupo,u.usuario,u.senha_hash,
                COALESCE(g.nome,'Administração'),u.is_admin
                FROM app_usuario u LEFT JOIN grupo g ON g.id_grupo=u.id_grupo
                WHERE lower(u.usuario)=lower(%s) AND u.ativo=TRUE""", (username,))
            user = cur.fetchone()
        if user and check_password_hash(user[3], password):
            session.clear()
            session.update(id_usuario=user[0], id_grupo=user[1], usuario=user[2], nome_grupo=user[4], is_admin=user[5])
            return redirect(url_for("admin_acessos" if user[5] else "inicio"))
        flash("Usuário ou senha inválidos.", "erro")
    return render_template("login.html")


@app.post("/sair")
def sair():
    session.clear()
    return redirect(url_for("login"))


@app.route("/admin/acessos")
def admin_acessos():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT g.id_grupo,g.nome,u.id_usuario,u.usuario,u.ativo
            FROM grupo g LEFT JOIN app_usuario u ON u.id_grupo=g.id_grupo AND u.is_admin=FALSE
            ORDER BY lower(g.nome),g.id_grupo""")
        acessos = cur.fetchall()
        cur.execute("SELECT id_grupo,nome FROM grupo ORDER BY lower(nome),id_grupo")
        grupos = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM grupo")
        total_grupos = cur.fetchone()[0]
    grupos_sem_acesso = [grupo for grupo in grupos if not any(row[0] == grupo[0] and row[2] is not None for row in acessos)]
    return render_template("admin_acessos.html", acessos=acessos, grupos=grupos_sem_acesso, total_grupos=total_grupos, limite_grupos=15)


@app.post("/admin/acessos/novo")
def admin_novo_acesso():
    username = request.form.get("usuario", "").strip()
    password = request.form.get("senha", "")
    group_id = request.form.get("id_grupo", "").strip()
    new_group_name = request.form.get("novo_grupo", "").strip()
    if bool(group_id) == bool(new_group_name):
        flash("Escolha um grupo existente ou informe o nome de um novo grupo.", "erro")
        return redirect(url_for("admin_acessos"))
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
        flash("O usuário precisa ter de 3 a 64 caracteres (letras, números, ponto, hífen ou sublinhado).", "erro")
        return redirect(url_for("admin_acessos"))
    if len(password) < 12:
        flash("A senha precisa ter pelo menos 12 caracteres.", "erro")
        return redirect(url_for("admin_acessos"))
    if new_group_name and (len(new_group_name) > 100 or not new_group_name.strip()):
        flash("Informe um nome de grupo válido, com até 100 caracteres.", "erro")
        return redirect(url_for("admin_acessos"))
    try:
        with connect() as conn, conn.cursor() as cur:
            if new_group_name:
                cur.execute("SELECT COUNT(*) FROM grupo")
                if cur.fetchone()[0] >= 15:
                    flash("O sistema permite no máximo 15 grupos.", "erro")
                    return redirect(url_for("admin_acessos"))
                cur.execute("SELECT 1 FROM grupo WHERE lower(nome)=lower(%s)", (new_group_name,))
                if cur.fetchone():
                    flash("Já existe um grupo com esse nome.", "erro")
                    return redirect(url_for("admin_acessos"))
                cur.execute("INSERT INTO grupo (nome) VALUES (%s) RETURNING id_grupo", (new_group_name,))
                group_id = cur.fetchone()[0]
            else:
                group_id = int(group_id)
                cur.execute("SELECT 1 FROM grupo WHERE id_grupo=%s", (group_id,))
                if not cur.fetchone():
                    flash("Grupo não encontrado.", "erro")
                    return redirect(url_for("admin_acessos"))
            cur.execute("SELECT 1 FROM app_usuario WHERE id_grupo=%s AND is_admin=FALSE", (group_id,))
            if cur.fetchone():
                flash("Esse grupo já tem um acesso. Use a opção de editar para trocar usuário ou senha.", "erro")
                return redirect(url_for("admin_acessos"))
            cur.execute("INSERT INTO app_usuario (id_grupo,usuario,senha_hash,is_admin) VALUES (%s,%s,%s,FALSE)",
                        (group_id, username, generate_password_hash(password)))
        flash("Acesso do grupo criado.", "sucesso")
    except psycopg.errors.UniqueViolation:
        flash("Esse usuário já está em uso.", "erro")
    except Exception:
        app.logger.exception("Falha ao criar acesso de grupo")
        flash("Não foi possível criar o acesso.", "erro")
    return redirect(url_for("admin_acessos"))


@app.route("/admin/acessos/<int:user_id>/editar", methods=["GET", "POST"])
def admin_editar_acesso(user_id):
    if request.method == "POST":
        username = request.form.get("usuario", "").strip()
        password = request.form.get("senha", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            flash("O usuário precisa ter de 3 a 64 caracteres válidos.", "erro")
            return redirect(url_for("admin_editar_acesso", user_id=user_id))
        if password and len(password) < 12:
            flash("A nova senha precisa ter pelo menos 12 caracteres.", "erro")
            return redirect(url_for("admin_editar_acesso", user_id=user_id))
        try:
            with connect() as conn, conn.cursor() as cur:
                if password:
                    cur.execute("UPDATE app_usuario SET usuario=%s,senha_hash=%s WHERE id_usuario=%s AND is_admin=FALSE",
                                (username, generate_password_hash(password), user_id))
                else:
                    cur.execute("UPDATE app_usuario SET usuario=%s WHERE id_usuario=%s AND is_admin=FALSE", (username,user_id))
                if cur.rowcount == 0:
                    flash("Acesso não encontrado.", "erro")
                    return redirect(url_for("admin_acessos"))
            flash("Acesso atualizado.", "sucesso")
            return redirect(url_for("admin_acessos"))
        except psycopg.errors.UniqueViolation:
            flash("Esse usuário já está em uso.", "erro")
        except Exception:
            app.logger.exception("Falha ao editar acesso")
            flash("Não foi possível atualizar o acesso.", "erro")
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT u.id_usuario,u.usuario,g.nome FROM app_usuario u
            JOIN grupo g ON g.id_grupo=u.id_grupo WHERE u.id_usuario=%s AND u.is_admin=FALSE""", (user_id,))
        acesso = cur.fetchone()
    if not acesso:
        flash("Acesso não encontrado.", "erro")
        return redirect(url_for("admin_acessos"))
    return render_template("admin_editar_acesso.html", acesso=acesso)


@app.post("/admin/acessos/<int:user_id>/status")
def admin_alterar_status(user_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE app_usuario SET ativo=NOT ativo WHERE id_usuario=%s AND is_admin=FALSE", (user_id,))
        if cur.rowcount:
            flash("Situação do acesso atualizada.", "sucesso")
        else:
            flash("Acesso não encontrado.", "erro")
    return redirect(url_for("admin_acessos"))


@app.route("/admin/grupos")
def admin_grupos():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT g.id_grupo,g.nome,u.id_usuario,u.usuario,u.ativo
            FROM grupo g LEFT JOIN app_usuario u ON u.id_grupo=g.id_grupo AND u.is_admin=FALSE
            ORDER BY lower(g.nome),g.id_grupo""")
        grupos = cur.fetchall()
        cur.execute("SELECT COUNT(*) FROM grupo")
        total_grupos = cur.fetchone()[0]
    return render_template("admin_grupos.html", grupos=grupos, total_grupos=total_grupos, limite_grupos=15)


@app.post("/admin/grupos/novo")
def admin_novo_grupo():
    nome = request.form.get("nome", "").strip()
    if not nome or len(nome) > 100:
        flash("Informe o nome do grupo, com até 100 caracteres.", "erro")
        return redirect(url_for("admin_grupos"))
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM grupo")
            if cur.fetchone()[0] >= 15:
                flash("O sistema permite no máximo 15 grupos.", "erro")
                return redirect(url_for("admin_grupos"))
            cur.execute("SELECT 1 FROM grupo WHERE lower(nome)=lower(%s)", (nome,))
            if cur.fetchone():
                flash("Já existe um grupo com esse nome.", "erro")
                return redirect(url_for("admin_grupos"))
            cur.execute("INSERT INTO grupo (nome) VALUES (%s)", (nome,))
        flash("Grupo criado. Agora você pode adicionar o acesso do líder.", "sucesso")
    except Exception:
        app.logger.exception("Falha ao criar grupo")
        flash("Não foi possível criar o grupo.", "erro")
    return redirect(url_for("admin_grupos"))


@app.post("/admin/grupos/<int:group_id>/renomear")
def admin_renomear_grupo(group_id):
    nome = request.form.get("nome", "").strip()
    if not nome or len(nome) > 100:
        flash("Informe um nome válido, com até 100 caracteres.", "erro")
        return redirect(url_for("admin_grupos"))
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM grupo WHERE lower(nome)=lower(%s) AND id_grupo<>%s", (nome,group_id))
            if cur.fetchone():
                flash("Já existe outro grupo com esse nome.", "erro")
                return redirect(url_for("admin_grupos"))
            cur.execute("UPDATE grupo SET nome=%s WHERE id_grupo=%s", (nome,group_id))
            if not cur.rowcount:
                flash("Grupo não encontrado.", "erro")
                return redirect(url_for("admin_grupos"))
        flash("Nome do grupo atualizado.", "sucesso")
    except Exception:
        app.logger.exception("Falha ao renomear grupo")
        flash("Não foi possível renomear o grupo.", "erro")
    return redirect(url_for("admin_grupos"))


@app.post("/admin/grupos/<int:group_id>/excluir")
def admin_excluir_grupo(group_id):
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT nome FROM grupo WHERE id_grupo=%s FOR UPDATE", (group_id,))
            grupo = cur.fetchone()
            if not grupo:
                flash("Grupo não encontrado.", "erro")
                return redirect(url_for("admin_grupos"))
            cur.execute("""SELECT
                EXISTS(SELECT 1 FROM app_usuario WHERE id_grupo=%s),
                EXISTS(SELECT 1 FROM app_pessoa WHERE id_grupo=%s),
                EXISTS(SELECT 1 FROM app_atividade WHERE id_grupo=%s),
                EXISTS(SELECT 1 FROM app_venda WHERE id_grupo=%s),
                EXISTS(SELECT 1 FROM movimentacao WHERE id_grupo=%s),
                EXISTS(SELECT 1 FROM atividade WHERE id_grupo=%s)""",
                (group_id,group_id,group_id,group_id,group_id,group_id))
            if any(cur.fetchone()):
                flash("Este grupo já tem um acesso ou dados associados. Renomeie-o em vez de excluir para preservar os registros.", "erro")
                return redirect(url_for("admin_grupos"))
            cur.execute("DELETE FROM grupo WHERE id_grupo=%s", (group_id,))
        flash("Grupo vazio excluído.", "sucesso")
    except Exception:
        app.logger.exception("Falha ao excluir grupo")
        flash("Não foi possível excluir o grupo. Ele pode estar vinculado a outros dados.", "erro")
    return redirect(url_for("admin_grupos"))


def connect():
    return psycopg.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "IEAD_Maria_ISABEL_Grupos"),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


def initialize_extra_tables():
    """Additive tables for activities and partial sale payments."""
    statements = [
        """CREATE TABLE IF NOT EXISTS app_atividade (
            id_atividade BIGSERIAL PRIMARY KEY,
            id_grupo INTEGER NOT NULL DEFAULT 1,
            nome VARCHAR(120) NOT NULL,
            data_atividade DATE,
            descricao TEXT,
            ativa BOOLEAN NOT NULL DEFAULT TRUE,
            criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS app_venda (
            id_venda BIGSERIAL PRIMARY KEY,
            id_grupo INTEGER NOT NULL DEFAULT 1,
            id_atividade BIGINT REFERENCES app_atividade(id_atividade) ON DELETE SET NULL,
            comprador VARCHAR(120) NOT NULL,
            produto VARCHAR(120) NOT NULL,
            quantidade NUMERIC(12,2) NOT NULL CHECK (quantidade > 0),
            valor_unitario NUMERIC(12,2) NOT NULL CHECK (valor_unitario > 0),
            data_venda DATE NOT NULL DEFAULT CURRENT_DATE,
            observacao TEXT,
            criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS app_pagamento (
            id_pagamento BIGSERIAL PRIMARY KEY,
            id_venda BIGINT NOT NULL REFERENCES app_venda(id_venda) ON DELETE CASCADE,
            id_movimentacao INTEGER NOT NULL,
            valor NUMERIC(12,2) NOT NULL CHECK (valor > 0),
            data_pagamento DATE NOT NULL DEFAULT CURRENT_DATE,
            forma VARCHAR(40) NOT NULL DEFAULT 'Dinheiro',
            criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS app_despesa_atividade (
            id_movimentacao BIGINT PRIMARY KEY,
            id_atividade BIGINT NOT NULL REFERENCES app_atividade(id_atividade) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS app_pessoa (
            id_pessoa BIGSERIAL PRIMARY KEY,
            nome VARCHAR(160) NOT NULL,
            telefone VARCHAR(40),
            endereco TEXT,
            data_nascimento DATE,
            criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""",
    ]
    with connect() as conn:
        with conn.cursor() as cur:
            # Group names and records are managed by the administrator.
            for statement in statements:
                cur.execute(statement)
            cur.execute("ALTER TABLE app_pessoa ADD COLUMN IF NOT EXISTS id_grupo INTEGER")
            cur.execute("UPDATE app_pessoa SET id_grupo=1 WHERE id_grupo IS NULL")
            cur.execute("ALTER TABLE app_pessoa ALTER COLUMN id_grupo SET DEFAULT 1")
            cur.execute("ALTER TABLE app_pessoa ALTER COLUMN id_grupo SET NOT NULL")
            cur.execute("""DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='app_pessoa_id_grupo_fkey') THEN
                    ALTER TABLE app_pessoa ADD CONSTRAINT app_pessoa_id_grupo_fkey
                    FOREIGN KEY (id_grupo) REFERENCES grupo(id_grupo);
                END IF;
            END $$""")
            cur.execute("""CREATE TABLE IF NOT EXISTS app_usuario (
                id_usuario BIGSERIAL PRIMARY KEY,
                id_grupo INTEGER UNIQUE REFERENCES grupo(id_grupo),
                usuario VARCHAR(64) NOT NULL UNIQUE,
                senha_hash TEXT NOT NULL,
                is_admin BOOLEAN NOT NULL DEFAULT FALSE,
                ativo BOOLEAN NOT NULL DEFAULT TRUE,
                criado_em TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            cur.execute("ALTER TABLE app_usuario ALTER COLUMN id_grupo DROP NOT NULL")
            cur.execute("ALTER TABLE app_usuario ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE")
            cur.execute("""DO $$ DECLARE c RECORD; BEGIN
                FOR c IN SELECT conname FROM pg_constraint
                    WHERE conrelid='app_usuario'::regclass AND contype='u'
                    AND pg_get_constraintdef(oid)='UNIQUE (id_grupo)'
                LOOP EXECUTE format('ALTER TABLE app_usuario DROP CONSTRAINT %I',c.conname); END LOOP;
            END $$""")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS app_usuario_id_grupo_uq ON app_usuario (id_grupo) WHERE id_grupo IS NOT NULL")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS app_usuario_usuario_lower_uq ON app_usuario (lower(usuario))")


def money(value):
    try:
        raw = str(value).strip().replace("R$", "").replace(" ", "")
        if "," in raw and "." in raw:
            raw = raw.replace(".", "").replace(",", ".")
        elif "," in raw:
            raw = raw.replace(",", ".")
        number = Decimal(raw)
        if not number.is_finite() or number <= 0:
            raise ValueError
        return number.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        raise ValueError("Informe um valor maior que zero.")


def fetch_one(sql, params=()):
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


CATEGORY_NAMES = {
    2: "Oferta", 3: "Venda", 4: "Doação em dinheiro",
    5: "Ingredientes", 6: "Alimentação", 7: "Decoração",
    8: "Contratação", 9: "Presente", 10: "Outros",
}


def categories(kind):
    first, last = (2, 4) if kind == "ENTRADA" else (5, 10)
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT id_categoria FROM categoria WHERE tipo=%s AND id_categoria BETWEEN %s AND %s ORDER BY id_categoria", (kind, first, last))
        return [(row[0], CATEGORY_NAMES.get(row[0], f"Categoria {row[0]}")) for row in cur.fetchall()]


def activities(active_only=True):
    query = "SELECT id_atividade, nome FROM app_atividade WHERE id_grupo=%s"
    if active_only:
        query += " AND ativa=TRUE"
    query += " ORDER BY nome"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, (current_group_id(),))
        return cur.fetchall()


def dashboard_data():
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT
            COALESCE(SUM(CASE WHEN c.tipo='ENTRADA' THEN m.valor ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN c.tipo='SAIDA' THEN m.valor ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN m.eh_saldo_inicial=TRUE THEN m.valor
                WHEN c.tipo='ENTRADA' THEN m.valor WHEN c.tipo='SAIDA' THEN -m.valor ELSE 0 END),0)
            FROM movimentacao m JOIN categoria c ON c.id_categoria=m.id_categoria
            WHERE m.id_grupo=%s""", (current_group_id(),))
        entradas, saidas, saldo = cur.fetchone()
        cur.execute("""SELECT COALESCE(SUM(v.quantidade*v.valor_unitario),0),
            COALESCE(SUM(COALESCE(p.pago,0)),0)
            FROM app_venda v LEFT JOIN (
              SELECT id_venda,SUM(valor) AS pago FROM app_pagamento GROUP BY id_venda
            ) p ON p.id_venda=v.id_venda WHERE v.id_grupo=%s""", (current_group_id(),))
        total_vendas, recebido = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM app_atividade WHERE id_grupo=%s AND ativa=TRUE", (current_group_id(),))
        qtd_atividades = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM app_pessoa WHERE id_grupo=%s", (current_group_id(),))
        qtd_membros = cur.fetchone()[0]
    return dict(entradas=entradas, saidas=saidas, saldo=saldo,
                total_vendas=total_vendas, recebido=recebido,
                pendente=total_vendas-recebido, qtd_atividades=qtd_atividades,
                qtd_membros=qtd_membros)


@app.route("/")
def inicio():
    try:
        return render_template("index.html", resumo=dashboard_data())
    except Exception as exc:
        app.logger.exception("Falha ao carregar o painel")
        flash("Não foi possível carregar os dados do PostgreSQL. Confira se o serviço está ligado e as configurações locais.", "erro")
        return render_template("index.html", resumo=None)


@app.route("/nova-entrada", methods=["GET", "POST"])
def nova_entrada():
    if request.method == "POST":
        try:
            valor = money(request.form.get("valor", ""))
            with connect() as conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO movimentacao
                    (id_grupo,id_categoria,data_movimentacao,descricao,valor)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (current_group_id(), request.form["id_categoria"], request.form["data"], request.form["descricao"].strip(), valor))
            flash("Entrada registrada.", "sucesso")
            return redirect(url_for("movimentacoes"))
        except (ValueError, KeyError) as exc:
            flash(str(exc) or "Confira os dados informados.", "erro")
        except Exception:
            app.logger.exception("Falha ao registrar entrada")
            flash("Não foi possível registrar a entrada. Confira a categoria e a conexão com o banco.", "erro")
    try:
        return render_template("form_movimento.html", titulo="Nova entrada", tipo="entrada", categorias=categories("ENTRADA"), atividades=activities())
    except Exception:
        app.logger.exception("Falha ao carregar categorias")
        flash("Não foi possível carregar as categorias do banco.", "erro")
        return redirect(url_for("inicio"))


@app.route("/nova-despesa", methods=["GET", "POST"])
def nova_despesa():
    if request.method == "POST":
        try:
            valor = money(request.form.get("valor", ""))
            atividade_id = request.form.get("id_atividade") or None
            if atividade_id and not fetch_one("SELECT 1 FROM app_atividade WHERE id_atividade=%s AND id_grupo=%s", (atividade_id,current_group_id())):
                raise ValueError("Atividade não encontrada neste grupo.")
            with connect() as conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO movimentacao
                    (id_grupo,id_categoria,data_movimentacao,descricao,valor)
                    VALUES (%s,%s,%s,%s,%s) RETURNING id_movimentacao""",
                    (current_group_id(), request.form["id_categoria"], request.form["data"], request.form["descricao"].strip(), valor))
                movement_id = cur.fetchone()[0]
                cur.execute("INSERT INTO despesa (id_atividade,id_movimentacao,descricao,valor) VALUES (%s,%s,%s,%s)",
                    (None, movement_id, request.form["descricao"].strip(), valor))
                if atividade_id:
                    cur.execute("INSERT INTO app_despesa_atividade (id_movimentacao,id_atividade) VALUES (%s,%s)", (movement_id, atividade_id))
            flash("Despesa registrada.", "sucesso")
            return redirect(url_for("movimentacoes"))
        except (ValueError, KeyError) as exc:
            flash(str(exc) or "Confira os dados informados.", "erro")
        except Exception:
            app.logger.exception("Falha ao registrar despesa")
            flash("Não foi possível registrar a despesa. Confira a categoria e a conexão com o banco.", "erro")
    try:
        return render_template("form_movimento.html", titulo="Nova despesa", tipo="despesa", categorias=categories("SAIDA"), atividades=activities())
    except Exception:
        app.logger.exception("Falha ao carregar categorias")
        flash("Não foi possível carregar as categorias do banco.", "erro")
        return redirect(url_for("inicio"))


@app.route("/ajustar-saldo-inicial", methods=["GET", "POST"])
def ajustar_saldo_inicial():
    categorias = [(id_categoria, nome, "ENTRADA") for id_categoria, nome in categories("ENTRADA")]
    categorias += [(id_categoria, nome, "SAIDA") for id_categoria, nome in categories("SAIDA")]
    categorias_validas = {id_categoria: tipo for id_categoria, _nome, tipo in categorias}
    if request.method == "POST":
        try:
            valor = money(request.form.get("valor", ""))
            categoria_id = int(request.form.get("id_categoria", ""))
            categoria_tipo = categorias_validas.get(categoria_id)
            if not categoria_tipo:
                raise ValueError("Escolha uma categoria válida para o ajuste.")
            descricao = request.form.get("descricao", "").strip()
            if not descricao:
                raise ValueError("Informe o motivo do ajuste.")
            data_ajuste = date.fromisoformat(request.form.get("data", ""))
            with connect() as conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO movimentacao
                    (id_grupo,id_categoria,data_movimentacao,descricao,valor)
                    VALUES (%s,%s,%s,%s,%s)""",
                    (current_group_id(),categoria_id,data_ajuste,
                     f"Ajuste do saldo inicial: {descricao}",valor))
            flash("Ajuste registrado. O saldo inicial original foi preservado.", "sucesso")
            return redirect(url_for("movimentacoes"))
        except (ValueError, TypeError) as exc:
            flash(str(exc) or "Confira os dados do ajuste.", "erro")
        except Exception:
            app.logger.exception("Falha ao ajustar o saldo inicial")
            flash("Não foi possível registrar o ajuste.", "erro")
    return render_template("ajustar_saldo.html", categorias=categorias,
                           today=date.today().isoformat())


@app.route("/movimentacoes")
def movimentacoes():
    inicio = request.args.get("de") or ""
    fim = request.args.get("ate") or ""
    tipo = request.args.get("tipo", "")
    if tipo not in ("ENTRADA", "SAIDA"):
        tipo = ""
    categorias_filtro = categories(tipo) if tipo else categories("ENTRADA") + categories("SAIDA")
    categoria_param = request.args.get("categoria", "")
    categorias_validas = {str(category_id): category_id for category_id, _name in categorias_filtro}
    categoria_selecionada = categorias_validas.get(categoria_param)
    sql = """SELECT m.id_movimentacao,m.data_movimentacao,m.descricao,m.valor,c.tipo,c.id_categoria,m.eh_saldo_inicial
             FROM movimentacao m JOIN categoria c ON c.id_categoria=m.id_categoria
             WHERE m.id_grupo=%s"""
    params = [current_group_id()]
    if inicio:
        sql += " AND m.data_movimentacao >= %s"; params.append(inicio)
    if fim:
        sql += " AND m.data_movimentacao <= %s"; params.append(fim)
    if tipo:
        sql += " AND c.tipo = %s"; params.append(tipo)
    if categoria_selecionada is not None:
        sql += " AND c.id_categoria = %s"; params.append(categoria_selecionada)
    sql += " ORDER BY m.data_movimentacao DESC,m.id_movimentacao DESC"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params); registros = cur.fetchall()
    linhas = [(row[0], row[1], row[2], row[3], row[4], row[5], CATEGORY_NAMES.get(row[5], f"Categoria {row[5]}"), row[6]) for row in registros]
    return render_template("movimentacoes.html", linhas=linhas, de=inicio, ate=fim, tipo=tipo,
        categorias=categorias_filtro, categoria_selecionada=categoria_selecionada, filtro_tipo=tipo)


@app.get("/movimentacoes/pdf")
def pdf_movimentacoes():
    de_texto = request.args.get("de", "").strip()
    ate_texto = request.args.get("ate", "").strip()
    tipo = request.args.get("tipo", "").strip().upper()
    tipo = tipo if tipo in {"ENTRADA", "SAIDA"} else ""
    try:
        data_de = date.fromisoformat(de_texto) if de_texto else None
        data_ate = date.fromisoformat(ate_texto) if ate_texto else None
        if data_de and data_ate and data_de > data_ate:
            raise ValueError
    except ValueError:
        flash("Confira o período informado antes de gerar o PDF.", "erro")
        return redirect(url_for("movimentacoes"))
    categorias_filtro = categories(tipo) if tipo else categories("ENTRADA") + categories("SAIDA")
    valid_categories = {str(category_id): category_id for category_id, _name in categorias_filtro}
    category_id = valid_categories.get(request.args.get("categoria", ""))
    query = """SELECT m.data_movimentacao,m.descricao,m.valor,c.tipo,c.id_categoria,m.eh_saldo_inicial
        FROM movimentacao m JOIN categoria c ON c.id_categoria=m.id_categoria
        WHERE m.id_grupo=%s"""
    params = [current_group_id()]
    if data_de:
        query += " AND m.data_movimentacao >= %s"; params.append(data_de)
    if data_ate:
        query += " AND m.data_movimentacao <= %s"; params.append(data_ate)
    if tipo:
        query += " AND c.tipo=%s"; params.append(tipo)
    if category_id is not None:
        query += " AND c.id_categoria=%s"; params.append(category_id)
    query += " ORDER BY m.data_movimentacao,m.id_movimentacao"
    with connect() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        registros = cur.fetchall()
    entrada_total = sum((row[2] for row in registros if row[3] == "ENTRADA" or row[5]), Decimal("0"))
    saida_total = sum((row[2] for row in registros if row[3] == "SAIDA" and not row[5]), Decimal("0"))
    linhas = [[row[0].strftime("%d/%m/%Y"), row[1], CATEGORY_NAMES.get(row[4], f"Categoria {row[4]}"),
               "Saldo inicial" if row[5] else ("Entrada" if row[3] == "ENTRADA" else "Saída"), format_brl(row[2])]
              for row in registros]
    periodo = "Período: " + (f"{data_de.strftime('%d/%m/%Y') if data_de else 'início'} a {data_ate.strftime('%d/%m/%Y') if data_ate else 'hoje'}")
    filtros = [periodo, f"Tipo: {'Entradas' if tipo == 'ENTRADA' else 'Saídas' if tipo == 'SAIDA' else 'Todos'}"]
    if category_id is not None:
        filtros.append(f"Categoria: {CATEGORY_NAMES.get(category_id, f'Categoria {category_id}')}")
    return report_pdf_response("Relatório de movimentações", "Movimentações do caixa conforme os filtros selecionados.",
        ["Data", "Descrição", "Categoria", "Tipo", "Valor"], linhas,
        [("Entradas no relatório", format_brl(entrada_total)), ("Saídas no relatório", format_brl(saida_total)),
         ("Saldo líquido", format_brl(entrada_total-saida_total))],
        "relatorio_movimentacoes.pdf", [13, 40, 20, 12, 15], filtros)


@app.route("/movimentacoes/<int:movimentacao_id>/editar", methods=["GET", "POST"])
def editar_movimentacao(movimentacao_id):
    filtros = {
        "de": request.values.get("de", ""),
        "ate": request.values.get("ate", ""),
        "tipo": request.values.get("tipo", ""),
        "categoria": request.values.get("categoria", ""),
    }
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT m.data_movimentacao,m.descricao,m.valor,m.id_categoria,c.tipo,m.eh_saldo_inicial
                FROM movimentacao m JOIN categoria c ON c.id_categoria=m.id_categoria
                WHERE m.id_movimentacao=%s AND m.id_grupo=%s""", (movimentacao_id,current_group_id()))
            movimento = cur.fetchone()
            if not movimento:
                flash("Movimentação não encontrada.", "erro")
                return redirect(url_for("movimentacoes", **filtros))
            if movimento[5]:
                flash("O saldo inicial está protegido e não pode ser alterado por esta tela.", "erro")
                return redirect(url_for("movimentacoes", **filtros))
            cur.execute("SELECT id_pagamento FROM app_pagamento WHERE id_movimentacao=%s", (movimentacao_id,))
            pagamento = cur.fetchone()
        categoria_id = movimento[3]
        if request.method == "POST":
            descricao = request.form.get("descricao", "").strip()
            valor = money(request.form.get("valor", ""))
            data_mov = date.fromisoformat(request.form.get("data", ""))
            posted_category = int(request.form.get("id_categoria", categoria_id))
            if not descricao:
                raise ValueError("Informe a descrição da movimentação.")
            if pagamento and posted_category != categoria_id:
                raise ValueError("A categoria deste recebimento de venda não pode ser alterada.")
            with connect() as conn, conn.cursor() as cur:
                cur.execute("""SELECT c.tipo,m.eh_saldo_inicial FROM movimentacao m
                    JOIN categoria c ON c.id_categoria=m.id_categoria
                    WHERE m.id_movimentacao=%s AND m.id_grupo=%s FOR UPDATE OF m""",
                    (movimentacao_id,current_group_id()))
                atual = cur.fetchone()
                cur.execute("SELECT tipo FROM categoria WHERE id_categoria=%s", (posted_category,))
                categoria = cur.fetchone()
                if not atual or atual[1]:
                    raise ValueError("Movimentação não encontrada ou protegida.")
                if not categoria or categoria[0] != atual[0]:
                    raise ValueError("Escolha uma categoria do mesmo tipo da movimentação.")
                if pagamento:
                    cur.execute("SELECT id_venda FROM app_pagamento WHERE id_pagamento=%s", (pagamento[0],))
                    pagamento_atual = cur.fetchone()
                    if not pagamento_atual:
                        raise ValueError("O recebimento vinculado não foi encontrado.")
                    sale_id = pagamento_atual[0]
                    cur.execute("SELECT quantidade*valor_unitario FROM app_venda WHERE id_venda=%s", (sale_id,))
                    venda = cur.fetchone()
                    cur.execute("SELECT COALESCE(SUM(valor),0) FROM app_pagamento WHERE id_venda=%s AND id_pagamento<>%s", (sale_id,pagamento[0]))
                    outros_pagamentos = cur.fetchone()[0]
                    if not venda or valor > venda[0]-outros_pagamentos:
                        raise ValueError("O valor ultrapassa o saldo pendente desta venda.")
                    cur.execute("UPDATE app_pagamento SET valor=%s,data_pagamento=%s WHERE id_pagamento=%s", (valor,data_mov,pagamento[0]))
                elif atual[0] == "SAIDA":
                    cur.execute("UPDATE despesa SET descricao=%s,valor=%s WHERE id_movimentacao=%s", (descricao,valor,movimentacao_id))
                cur.execute("""UPDATE movimentacao SET id_categoria=%s,data_movimentacao=%s,descricao=%s,valor=%s
                    WHERE id_movimentacao=%s AND id_grupo=%s""",
                    (posted_category,data_mov,descricao,valor,movimentacao_id,current_group_id()))
            flash("Movimentação atualizada.", "sucesso")
            return redirect(url_for("movimentacoes", **filtros))
        categorias_mov = categories(movimento[4])
        return render_template("editar_movimentacao.html", movimento=(movimentacao_id,*movimento),
            categorias=categorias_mov, categoria_nome=CATEGORY_NAMES.get(movimento[3], f"Categoria {movimento[3]}"),
            eh_pagamento=bool(pagamento), filtros=filtros)
    except (ValueError, TypeError) as exc:
        flash(str(exc) or "Confira os dados da movimentação.", "erro")
    except Exception:
        app.logger.exception("Falha ao editar movimentação")
        flash("Não foi possível alterar a movimentação.", "erro")
    return redirect(url_for("movimentacoes", **filtros))


@app.post("/movimentacoes/<int:movimentacao_id>/excluir")
def excluir_movimentacao(movimentacao_id):
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT m.eh_saldo_inicial,c.tipo FROM movimentacao m
                JOIN categoria c ON c.id_categoria=m.id_categoria
                WHERE m.id_movimentacao=%s AND m.id_grupo=%s FOR UPDATE OF m""",
                (movimentacao_id, current_group_id()))
            registro = cur.fetchone()
            if not registro:
                flash("Movimentação não encontrada.", "erro")
                return redirect(url_for("movimentacoes"))
            if registro[0]:
                flash("O saldo inicial está protegido e não pode ser excluído por esta tela.", "erro")
                return redirect(url_for("movimentacoes"))
            if registro[1] == "SAIDA":
                cur.execute("DELETE FROM despesa WHERE id_movimentacao=%s", (movimentacao_id,))
                cur.execute("DELETE FROM app_despesa_atividade WHERE id_movimentacao=%s", (movimentacao_id,))
            else:
                cur.execute("DELETE FROM app_pagamento WHERE id_movimentacao=%s", (movimentacao_id,))
            cur.execute("DELETE FROM movimentacao WHERE id_movimentacao=%s AND id_grupo=%s", (movimentacao_id, current_group_id()))
        flash("Movimentação excluída.", "sucesso")
    except Exception:
        app.logger.exception("Falha ao excluir movimentação")
        flash("Não foi possível excluir a movimentação. Ela pode estar vinculada a outro registro.", "erro")
    return redirect(url_for("movimentacoes", de=request.args.get("de", ""), ate=request.args.get("ate", ""),
        tipo=request.args.get("tipo", ""), categoria=request.args.get("categoria", "")))


@app.route("/pessoas", methods=["GET", "POST"])
def pessoas():
    if request.method == "POST":
        nome = request.form.get("nome", "").strip()
        telefone = request.form.get("telefone", "").strip() or None
        endereco = request.form.get("endereco", "").strip() or None
        nascimento = request.form.get("data_nascimento") or None
        if telefone and len(telefone) > 11:
            flash("O telefone deve ter no máximo 11 caracteres.", "erro")
            return redirect(url_for("pessoas"))
        if not nome:
            flash("Informe o nome do membro.", "erro")
            return redirect(url_for("pessoas"))
        try:
            if nascimento:
                date.fromisoformat(nascimento)
            with connect() as conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO app_pessoa (id_grupo,nome,telefone,endereco,data_nascimento)
                    VALUES (%s,%s,%s,%s,%s)""", (current_group_id(),nome,telefone,endereco,nascimento))
            flash("Membro cadastrado.", "sucesso")
            return redirect(url_for("pessoas"))
        except ValueError:
            flash("Confira a data de nascimento informada.", "erro")
        except Exception:
            app.logger.exception("Falha ao cadastrar pessoa")
            flash("Não foi possível cadastrar a pessoa. Confira a conexão com o banco.", "erro")

    nome_filtro = request.args.get("nome", "").strip()
    idade_modo = request.args.get("idade_modo", "")
    if idade_modo not in ("", "exata", "faixa"):
        idade_modo = ""
    idade_texto = request.args.get("idade", "").strip()
    idade_de_texto = request.args.get("idade_de", "").strip()
    idade_ate_texto = request.args.get("idade_ate", "").strip()
    idade_exata = idade_de = idade_ate = None
    try:
        if idade_modo == "exata":
            idade_exata = int(idade_texto)
            if not 0 <= idade_exata <= 120:
                raise ValueError
        elif idade_modo == "faixa":
            idade_de, idade_ate = int(idade_de_texto), int(idade_ate_texto)
            if not 0 <= idade_de <= idade_ate <= 120:
                raise ValueError
    except (TypeError, ValueError):
        flash("Confira a idade ou a faixa de idade escolhida.", "erro")
        idade_modo = ""
        idade_exata = idade_de = idade_ate = None

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT id_pessoa,nome,telefone,endereco,data_nascimento
                FROM app_pessoa WHERE id_grupo=%s ORDER BY lower(nome),id_pessoa""", (current_group_id(),))
            pessoas_cadastradas = cur.fetchall()
        hoje = date.today()
        registros = []
        for id_pessoa,nome,telefone,endereco,nascimento in pessoas_cadastradas:
            idade = None
            if nascimento:
                idade = hoje.year - nascimento.year - ((hoje.month,hoje.day) < (nascimento.month,nascimento.day))
            if nome_filtro and nome_filtro.casefold() not in nome.casefold():
                continue
            if idade_modo == "exata" and idade != idade_exata:
                continue
            if idade_modo == "faixa" and (idade is None or not idade_de <= idade <= idade_ate):
                continue
            registros.append((id_pessoa,nome,telefone,endereco,nascimento,idade))
        return render_template("pessoas.html", registros=registros, total_membros=len(pessoas_cadastradas), nome_filtro=nome_filtro,
            idade_modo=idade_modo, idade_texto=idade_texto, idade_de_texto=idade_de_texto,
            idade_ate_texto=idade_ate_texto)
    except Exception:
        app.logger.exception("Falha ao carregar pessoas")
        flash("Não foi possível carregar os cadastros de pessoas.", "erro")
        return redirect(url_for("inicio"))


@app.get("/pessoas/pdf")
def pdf_membros():
    nome_filtro = request.args.get("nome", "").strip()
    idade_modo = request.args.get("idade_modo", "")
    idade_texto = request.args.get("idade", "").strip()
    idade_de_texto = request.args.get("idade_de", "").strip()
    idade_ate_texto = request.args.get("idade_ate", "").strip()
    idade_exata = idade_de = idade_ate = None
    try:
        if idade_modo == "exata":
            idade_exata = int(idade_texto)
            if not 0 <= idade_exata <= 120:
                raise ValueError
        elif idade_modo == "faixa":
            idade_de, idade_ate = int(idade_de_texto), int(idade_ate_texto)
            if not 0 <= idade_de <= idade_ate <= 120:
                raise ValueError
        else:
            idade_modo = ""
    except (TypeError, ValueError):
        idade_modo = ""
        idade_exata = idade_de = idade_ate = None
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT nome,telefone,endereco,data_nascimento FROM app_pessoa
            WHERE id_grupo=%s ORDER BY lower(nome),id_pessoa""", (current_group_id(),))
        cadastrados = cur.fetchall()
    hoje = date.today()
    registros = []
    for nome,telefone,endereco,nascimento in cadastrados:
        idade = None
        if nascimento:
            idade = hoje.year - nascimento.year - ((hoje.month,hoje.day) < (nascimento.month,nascimento.day))
        if nome_filtro and nome_filtro.casefold() not in nome.casefold():
            continue
        if idade_modo == "exata" and idade != idade_exata:
            continue
        if idade_modo == "faixa" and (idade is None or not idade_de <= idade <= idade_ate):
            continue
        registros.append([nome, formatar_telefone(telefone) if telefone else "-", endereco or "-",
                          nascimento.strftime("%d/%m/%Y") if nascimento else "-",
                          str(idade) if idade is not None else "-"])
    filtros = []
    if nome_filtro:
        filtros.append(f"Nome contém: {nome_filtro}")
    if idade_modo == "exata":
        filtros.append(f"Idade exata: {idade_exata} anos")
    elif idade_modo == "faixa":
        filtros.append(f"Faixa de idade: {idade_de} a {idade_ate} anos")
    if not filtros:
        filtros.append("Filtros: todos os membros")
    return report_pdf_response("Lista de membros", "Cadastro de membros do grupo.",
        ["Nome", "Telefone", "Endereço", "Data de nascimento", "Idade"], registros,
        [("Membros neste relatório", str(len(registros)))], "relatorio_membros.pdf", [24, 17, 34, 17, 8], filtros)


@app.route("/pessoas/<int:id_pessoa>/editar", methods=["GET", "POST"])
def editar_membro(id_pessoa):
    if request.method == "POST":
        nome=request.form.get("nome", "").strip()
        telefone=request.form.get("telefone", "").strip() or None
        endereco=request.form.get("endereco", "").strip() or None
        nascimento=request.form.get("data_nascimento") or None
        if telefone and len(telefone) > 11:
            flash("O telefone deve ter no máximo 11 caracteres.", "erro")
            return redirect(url_for("editar_membro", id_pessoa=id_pessoa))
        if not nome:
            flash("Informe o nome do membro.", "erro")
            return redirect(url_for("editar_membro", id_pessoa=id_pessoa))
        try:
            if nascimento:
                date.fromisoformat(nascimento)
            with connect() as conn, conn.cursor() as cur:
                cur.execute("""UPDATE app_pessoa SET nome=%s,telefone=%s,endereco=%s,data_nascimento=%s
                    WHERE id_pessoa=%s AND id_grupo=%s""", (nome,telefone,endereco,nascimento,id_pessoa,current_group_id()))
                if cur.rowcount == 0:
                    flash("Membro não encontrado.", "erro")
                    return redirect(url_for("pessoas"))
            flash("Cadastro do membro atualizado.", "sucesso")
            return redirect(url_for("pessoas"))
        except ValueError:
            flash("Confira a data de nascimento informada.", "erro")
        except Exception:
            app.logger.exception("Falha ao alterar membro")
            flash("Não foi possível atualizar o cadastro.", "erro")
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id_pessoa,nome,telefone,endereco,data_nascimento FROM app_pessoa WHERE id_pessoa=%s AND id_grupo=%s", (id_pessoa,current_group_id()))
            membro=cur.fetchone()
        if not membro:
            flash("Membro não encontrado.", "erro")
            return redirect(url_for("pessoas"))
        return render_template("editar_membro.html", membro=membro)
    except Exception:
        app.logger.exception("Falha ao abrir cadastro de membro")
        flash("Não foi possível abrir o cadastro.", "erro")
        return redirect(url_for("pessoas"))


@app.post("/pessoas/<int:id_pessoa>/excluir")
def excluir_membro(id_pessoa):
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM app_pessoa WHERE id_pessoa=%s AND id_grupo=%s", (id_pessoa,current_group_id()))
            if cur.rowcount == 0:
                flash("Membro não encontrado.", "erro")
                return redirect(url_for("pessoas"))
        flash("Cadastro do membro excluído.", "sucesso")
    except Exception:
        app.logger.exception("Falha ao excluir membro")
        flash("Não foi possível excluir o cadastro do membro.", "erro")
    return redirect(url_for("pessoas"))


@app.route("/atividades", methods=["GET", "POST"])
def lista_atividades():
    if request.method == "POST":
        with connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO app_atividade (id_grupo,nome,data_atividade,descricao) VALUES (%s,%s,%s,%s)",
                (current_group_id(), request.form["nome"].strip(), request.form.get("data") or None, request.form.get("descricao", "").strip()))
        flash("Atividade criada.", "sucesso")
        return redirect(url_for("lista_atividades"))
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT a.id_atividade,a.nome,a.data_atividade,a.ativa,
            COALESCE(v.total_vendido,0),COALESCE(v.total_recebido,0),COALESCE(d.total_gasto,0)
            FROM app_atividade a LEFT JOIN (
              SELECT v.id_atividade,SUM(v.quantidade*v.valor_unitario) total_vendido,
                SUM(COALESCE(p.pago,0)) total_recebido FROM app_venda v
              LEFT JOIN (SELECT id_venda,SUM(valor) pago FROM app_pagamento GROUP BY id_venda) p ON p.id_venda=v.id_venda
              GROUP BY v.id_atividade) v ON v.id_atividade=a.id_atividade
            LEFT JOIN (SELECT da.id_atividade,SUM(m.valor) total_gasto FROM app_despesa_atividade da
              JOIN movimentacao m ON m.id_movimentacao=da.id_movimentacao GROUP BY da.id_atividade) d ON d.id_atividade=a.id_atividade
            WHERE a.id_grupo=%s ORDER BY a.ativa DESC,a.data_atividade DESC NULLS LAST,a.nome""", (current_group_id(),))
        linhas=cur.fetchall()
    return render_template("atividades.html", linhas=linhas)


@app.route("/atividades/<int:atividade_id>")
def atividade_detalhe(atividade_id):
    de = request.args.get("de", "").strip()
    ate = request.args.get("ate", "").strip()
    situacao = request.args.get("situacao", "").strip().lower()
    if situacao not in {"", "pendente", "pago", "parcial"}:
        situacao = ""
    try:
        data_de = date.fromisoformat(de) if de else None
        data_ate = date.fromisoformat(ate) if ate else None
        if data_de and data_ate and data_de > data_ate:
            raise ValueError
    except ValueError:
        flash("Confira o período informado: a data inicial deve ser válida e anterior à data final.", "erro")
        de = ate = ""
        data_de = data_ate = None

    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT nome,data_atividade,descricao FROM app_atividade WHERE id_atividade=%s AND id_grupo=%s",
                    (atividade_id,current_group_id()))
        atividade = cur.fetchone()
        if not atividade:
            flash("Atividade não encontrada neste grupo.", "erro")
            return redirect(url_for("lista_atividades"))
        filtros_data = []
        parametros = [current_group_id(), atividade_id]
        if data_de:
            filtros_data.append("AND v.data_venda >= %s")
            parametros.append(data_de)
        if data_ate:
            filtros_data.append("AND v.data_venda <= %s")
            parametros.append(data_ate)
        cur.execute(f"""SELECT v.id_venda,v.comprador,v.data_venda,v.produto,v.quantidade,
                v.quantidade*v.valor_unitario,COALESCE(p.pago,0)
            FROM app_venda v LEFT JOIN (
                SELECT id_venda,SUM(valor) pago FROM app_pagamento GROUP BY id_venda
            ) p ON p.id_venda=v.id_venda
            WHERE v.id_grupo=%s AND v.id_atividade=%s
            {' '.join(filtros_data)}
            ORDER BY v.data_venda DESC,v.id_venda DESC""", parametros)
        vendas_atividade = cur.fetchall()
    linhas = []
    for venda_id,nome,data_venda,produto,quantidade,total,recebido in vendas_atividade:
        pendente = total-recebido
        situacao = "Pago" if pendente <= 0 else ("Parcial" if recebido > 0 else "Pendente")
        linhas.append((venda_id,nome,data_venda,produto,quantidade,total,recebido,pendente,situacao))
    if situacao_filtro := request.args.get("situacao", "").strip().lower():
        if situacao_filtro in {"pendente", "pago", "parcial"}:
            linhas = [linha for linha in linhas if linha[8].lower() == situacao_filtro]
    totais = {
        "vendido": sum((linha[5] for linha in linhas), Decimal("0")),
        "recebido": sum((linha[6] for linha in linhas), Decimal("0")),
        "pendente": sum((linha[7] for linha in linhas), Decimal("0")),
    }
    por_produto = {}
    for linha in linhas:
        produto = linha[3] or "Sem produto informado"
        resumo = por_produto.setdefault(produto, {"quantidade": Decimal("0"), "vendido": Decimal("0"), "recebido": Decimal("0"), "pendente": Decimal("0")})
        resumo["quantidade"] += linha[4]
        resumo["vendido"] += linha[5]
        resumo["recebido"] += linha[6]
        resumo["pendente"] += linha[7]
    produtos = sorted(por_produto.items(), key=lambda item: item[0].casefold())
    quantidade_total = sum((resumo["quantidade"] for _, resumo in produtos), Decimal("0"))
    return render_template("atividade_detalhe.html", atividade=atividade, linhas=linhas, totais=totais,
                           produtos=produtos, quantidade_total=quantidade_total,
                           de=de, ate=ate, situacao=request.args.get("situacao", "").strip().lower())


@app.get("/atividades/<int:atividade_id>/pdf")
def pdf_atividade(atividade_id):
    de = request.args.get("de", "").strip()
    ate = request.args.get("ate", "").strip()
    situacao_filtro = request.args.get("situacao", "").strip().lower()
    if situacao_filtro not in {"", "pendente", "pago", "parcial"}:
        situacao_filtro = ""
    try:
        data_de = date.fromisoformat(de) if de else None
        data_ate = date.fromisoformat(ate) if ate else None
        if data_de and data_ate and data_de > data_ate:
            raise ValueError
    except ValueError:
        flash("Confira o período informado antes de gerar o PDF.", "erro")
        return redirect(url_for("atividade_detalhe", atividade_id=atividade_id))
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT nome,data_atividade,descricao FROM app_atividade WHERE id_atividade=%s AND id_grupo=%s",
                    (atividade_id,current_group_id()))
        atividade = cur.fetchone()
        if not atividade:
            flash("Atividade não encontrada neste grupo.", "erro")
            return redirect(url_for("lista_atividades"))
        filtros_sql = []
        params = [current_group_id(),atividade_id]
        if data_de:
            filtros_sql.append("AND v.data_venda >= %s"); params.append(data_de)
        if data_ate:
            filtros_sql.append("AND v.data_venda <= %s"); params.append(data_ate)
        cur.execute(f"""SELECT v.comprador,v.data_venda,v.produto,v.quantidade,
                v.quantidade*v.valor_unitario,COALESCE(p.pago,0)
            FROM app_venda v LEFT JOIN (
                SELECT id_venda,SUM(valor) pago FROM app_pagamento GROUP BY id_venda
            ) p ON p.id_venda=v.id_venda
            WHERE v.id_grupo=%s AND v.id_atividade=%s {' '.join(filtros_sql)}
            ORDER BY v.data_venda,v.id_venda""", params)
        vendas_pdf = cur.fetchall()
    linhas = []
    somas_produtos = {}
    for comprador,data_venda,produto,quantidade,total,recebido in vendas_pdf:
        pendente = total - recebido
        situacao = "Pago" if pendente <= 0 else ("Parcial" if recebido > 0 else "Pendente")
        if situacao_filtro and situacao.lower() != situacao_filtro:
            continue
        produto = produto or "Sem produto informado"
        linhas.append([comprador, data_venda.strftime("%d/%m/%Y"), produto,
                       format_quantity(quantidade), format_brl(total), format_brl(recebido),
                       format_brl(pendente), situacao])
        agregado = somas_produtos.setdefault(produto, {"qtd": Decimal("0"), "total": Decimal("0"),
                                                        "recebido": Decimal("0"), "pendente": Decimal("0")})
        agregado["qtd"] += quantidade
        agregado["total"] += total
        agregado["recebido"] += recebido
        agregado["pendente"] += pendente
    total_vendido = sum((item["total"] for item in somas_produtos.values()), Decimal("0"))
    total_recebido = sum((item["recebido"] for item in somas_produtos.values()), Decimal("0"))
    total_pendente = sum((item["pendente"] for item in somas_produtos.values()), Decimal("0"))
    qtd_total = sum((item["qtd"] for item in somas_produtos.values()), Decimal("0"))
    resumo_produtos = [[produto, format_quantity(item["qtd"]), format_brl(item["total"]),
                        format_brl(item["recebido"]), format_brl(item["pendente"])]
                       for produto,item in sorted(somas_produtos.items(), key=lambda pair: pair[0].casefold())]
    resumo_produtos.append(["Total geral", format_quantity(qtd_total), format_brl(total_vendido),
                            format_brl(total_recebido), format_brl(total_pendente)])
    filtros = []
    if data_de or data_ate:
        filtros.append(f"Período: {data_de.strftime('%d/%m/%Y') if data_de else 'início'} a {data_ate.strftime('%d/%m/%Y') if data_ate else 'hoje'}")
    if situacao_filtro:
        filtros.append(f"Situação: {situacao_filtro.title()}")
    elif not filtros:
        filtros.append("Filtros: todas as vendas da atividade")
    activity_date = atividade[1].strftime("%d/%m/%Y") if atividade[1] else "Data não informada"
    subtitle = f"{activity_date} | {atividade[2] or 'Vendas registradas nesta atividade.'}"
    safe_name = f"relatorio_atividade_{atividade_id}.pdf"
    return report_pdf_response(f"Relatório: {atividade[0]}", subtitle,
        ["Comprador", "Data", "Produto", "Qtd.", "Total", "Recebido", "Pendente", "Situação"], linhas,
        [("Total vendido", format_brl(total_vendido)), ("Recebido", format_brl(total_recebido)),
         ("Pendente", format_brl(total_pendente))], safe_name, [18, 10, 19, 8, 12, 12, 12, 9], filtros,
        extra_section=("Somatória dos produtos vendidos",
                       ["Produto", "Quantidade", "Total vendido", "Recebido", "Pendente"],
                       resumo_produtos, [32, 15, 19, 17, 17], (1,2,3,4)))


@app.post("/atividades/<int:atividade_id>/arquivar")
def arquivar_atividade(atividade_id):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE app_atividade SET ativa=NOT ativa WHERE id_atividade=%s AND id_grupo=%s", (atividade_id,current_group_id()))
    flash("Situação da atividade atualizada.", "sucesso")
    return redirect(url_for("lista_atividades"))


@app.route("/vendas", methods=["GET", "POST"])
def vendas():
    if request.method == "POST":
        try:
            qtd=money(request.form.get("quantidade", "")); unit=money(request.form.get("valor_unitario", ""))
            atividade_id = request.form.get("id_atividade") or None
            if atividade_id and not fetch_one("SELECT 1 FROM app_atividade WHERE id_atividade=%s AND id_grupo=%s", (atividade_id,current_group_id())):
                raise ValueError("Atividade não encontrada neste grupo.")
            with connect() as conn, conn.cursor() as cur:
                cur.execute("""INSERT INTO app_venda (id_grupo,id_atividade,comprador,produto,quantidade,valor_unitario,data_venda,observacao)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (current_group_id(),atividade_id,
                  request.form["comprador"].strip(),request.form["produto"].strip(),qtd,unit,request.form["data"],request.form.get("observacao","").strip()))
            flash("Venda registrada. Registre os recebimentos na lista de vendas.", "sucesso")
            return redirect(url_for("vendas"))
        except (ValueError,KeyError) as exc:
            flash(str(exc) or "Confira os dados informados.", "erro")
    de = request.args.get("de", "").strip()
    ate = request.args.get("ate", "").strip()
    atividade_filtro = request.args.get("atividade", "").strip()
    situacao_filtro = request.args.get("situacao", "").strip().lower() or "abertas"
    try:
        data_de = date.fromisoformat(de) if de else None
        data_ate = date.fromisoformat(ate) if ate else None
        if data_de and data_ate and data_de > data_ate:
            raise ValueError
    except ValueError:
        flash("Confira o período informado: a data inicial deve ser válida e anterior à data final.", "erro")
        de = ate = ""
        data_de = data_ate = None
    if situacao_filtro not in {"abertas", "pendente", "pago", "parcial"}:
        situacao_filtro = "abertas"
    with connect() as conn, conn.cursor() as cur:
        where = ["v.id_grupo=%s"]
        parametros = [current_group_id()]
        if data_de:
            where.append("v.data_venda >= %s")
            parametros.append(data_de)
        if data_ate:
            where.append("v.data_venda <= %s")
            parametros.append(data_ate)
        if atividade_filtro == "sem_atividade":
            where.append("v.id_atividade IS NULL")
        elif atividade_filtro:
            try:
                atividade_id_filtro = int(atividade_filtro)
            except ValueError:
                atividade_id_filtro = None
            if atividade_id_filtro is not None:
                where.append("v.id_atividade=%s")
                parametros.append(atividade_id_filtro)
        cur.execute(f"""SELECT v.id_venda,v.data_venda,v.comprador,v.produto,v.quantidade,v.valor_unitario,
           v.quantidade*v.valor_unitario,COALESCE(p.pago,0),v.id_atividade
           FROM app_venda v LEFT JOIN (SELECT id_venda,SUM(valor) pago FROM app_pagamento GROUP BY id_venda) p ON p.id_venda=v.id_venda
           WHERE {' AND '.join(where)} ORDER BY v.data_venda DESC,v.id_venda DESC""", parametros)
        linhas=cur.fetchall()
    if situacao_filtro:
        def status_venda(linha):
            pendente = linha[6] - linha[7]
            return "pago" if pendente <= 0 else ("parcial" if linha[7] > 0 else "pendente")
        if situacao_filtro == "abertas":
            linhas = [linha for linha in linhas if linha[6] - linha[7] > 0]
        else:
            linhas = [linha for linha in linhas if status_venda(linha) == situacao_filtro]
    return render_template("vendas.html", linhas=linhas, atividades=activities(),
                           today=date.today().isoformat(), de=de, ate=ate,
                           atividade_filtro=atividade_filtro, situacao_filtro=situacao_filtro)


@app.post("/vendas/<int:venda_id>/pagamento")
def registrar_pagamento(venda_id):
    try:
        valor=money(request.form.get("valor", ""))
        with connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT v.comprador,v.produto,v.data_venda,v.quantidade*v.valor_unitario,
                COALESCE(SUM(p.valor),0) FROM app_venda v LEFT JOIN app_pagamento p ON p.id_venda=v.id_venda
                WHERE v.id_venda=%s AND v.id_grupo=%s GROUP BY v.id_venda""",(venda_id,current_group_id()))
            venda=cur.fetchone()
            if not venda: raise ValueError("Venda não encontrada.")
            if valor > venda[3]-venda[4]: raise ValueError("O pagamento não pode ser maior que o saldo pendente.")
            cur.execute("SELECT id_categoria FROM categoria WHERE tipo='ENTRADA' AND id_categoria=3")
            categoria=cur.fetchone()
            if not categoria: raise ValueError("A categoria de venda (id 3) não foi encontrada.")
            descricao=f"Pagamento venda #{venda_id} - {venda[0]} ({venda[1]})"
            cur.execute("""INSERT INTO movimentacao (id_grupo,id_categoria,data_movimentacao,descricao,valor)
              VALUES (%s,%s,%s,%s,%s) RETURNING id_movimentacao""",(current_group_id(),categoria[0],request.form["data"],descricao,valor))
            movement_id=cur.fetchone()[0]
            cur.execute("INSERT INTO app_pagamento (id_venda,id_movimentacao,valor,data_pagamento,forma) VALUES (%s,%s,%s,%s,%s)",
              (venda_id,movement_id,valor,request.form["data"],request.form.get("forma","Dinheiro")))
        flash("Pagamento registrado e lançado no caixa.", "sucesso")
    except (ValueError,KeyError) as exc:
        flash(str(exc) or "Confira os dados informados.", "erro")
    except Exception:
        app.logger.exception("Falha ao registrar pagamento")
        flash("Não foi possível registrar o pagamento.", "erro")
    return redirect(url_for("vendas"))


@app.route("/fechamento")
def fechamento():
    mes=request.args.get("mes") or date.today().strftime("%Y-%m")
    try:
        date.fromisoformat(mes+"-01")
    except ValueError:
        mes=date.today().strftime("%Y-%m")
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(SUM(CASE WHEN c.tipo='ENTRADA' THEN m.valor ELSE 0 END),0),
            COALESCE(SUM(CASE WHEN c.tipo='SAIDA' THEN m.valor ELSE 0 END),0)
            FROM movimentacao m JOIN categoria c ON c.id_categoria=m.id_categoria
            WHERE m.id_grupo=%s AND to_char(m.data_movimentacao,'YYYY-MM')=%s""",(current_group_id(),mes))
        entradas,saidas=cur.fetchone()
        cur.execute("""SELECT COALESCE(SUM(v.quantidade*v.valor_unitario-COALESCE(p.pago,0)),0)
            FROM app_venda v LEFT JOIN (SELECT id_venda,SUM(valor) pago FROM app_pagamento WHERE to_char(data_pagamento,'YYYY-MM')<=%s GROUP BY id_venda) p ON p.id_venda=v.id_venda
            WHERE v.id_grupo=%s AND to_char(v.data_venda,'YYYY-MM')<=%s""",(mes,current_group_id(),mes))
        pendente=cur.fetchone()[0]
    resumo=dict(entradas=entradas,saidas=saidas,resultado=entradas-saidas,mes=mes,pendente=pendente)
    return render_template("fechamento.html",resumo=resumo)


@app.errorhandler(500)
def erro_interno(_error):
    app.logger.exception("Erro inesperado na aplicação")
    flash("Ocorreu um erro. Confira o terminal do Flask para ver os detalhes.", "erro")
    return redirect(url_for("inicio"))


if __name__ == "__main__":
    try:
        initialize_extra_tables()
        print("Tabelas complementares conferidas.")
    except Exception:
        app.logger.exception("Não foi possível preparar as tabelas complementares")
    app.run(host="127.0.0.1", port=int(os.environ.get("APP_PORT", "5001")), debug=True)
