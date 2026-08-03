import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

def send_mail(smtp_server, smtp_port, account_address, account_password,
              sender, to, subject, text):
    """
    Envía un correo electrónico con un mensaje de texto plano.

    Args:
        smtp_server (str): dirección del servidor SMTP
        smtp_port (int): puerto del servidor SMTP
        account_address (str): cuenta de correo remitente
        account_password (str): contraseña de la cuenta remitente
        sender (str): nombre que aparecerá como remitente
        to (str): correo del destinatario
        subject (str): asunto del correo
        text (str): cuerpo del mensaje
    """
    msg = MIMEMultipart()
    msg['From'] = sender
    msg['To'] = to
    msg['Subject'] = subject
    msg.attach(MIMEText(text))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.ehlo()
        server.starttls()
        server.login(account_address, account_password)
        server.sendmail(sender, to, msg.as_string())
        server.quit()
        print(f"Email sent to {to} with subject '{subject}'")
    except Exception as e:
        print(f"Failed to send email to {to}. Error: {e}")
