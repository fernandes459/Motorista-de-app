import os
import re
from datetime import datetime, timedelta
from pydantic import BaseModel
from fastapi import FastAPI, Request, Response, HTTPException, Depends
import requests
from google.cloud import speech
import io
import firebase_admin
from firebase_admin import credentials, auth
from supabase import create_client, Client
import json # For loading Firebase credentials from string environment variable

app = FastAPI()

# --- Supabase Configuration ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
print("Supabase client initialized.")

# --- Firebase Admin SDK Initialization ---
# This loads Firebase service account credentials from an environment variable.
# On Render, set FIREBASE_CREDENTIALS_JSON to the full JSON string of your service account key.
FIREBASE_CREDENTIALS_JSON_STR = os.environ.get("FIREBASE_CREDENTIALS_JSON")

if FIREBASE_CREDENTIALS_JSON_STR:
    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON_STR)
        cred = credentials.Certificate(cred_dict)
        if not firebase_admin._apps: # Initialize only if not already initialized
            firebase_admin.initialize_app(cred)
        print("Firebase Admin SDK initialized successfully.")
    except Exception as e:
        print(f"Error initializing Firebase Admin SDK from environment variable: {e}")
        print("Frontend authentication requiring Firebase Admin SDK may not work correctly.")
        # Dependendo da criticidade, você pode querer levantar uma exceção aqui
        # para evitar que o aplicativo inicie sem autenticação funcional.
        # Por enquanto, apenas logamos o erro.
else:
    print("FIREBASE_CREDENTIALS_JSON environment variable not found. Frontend authentication requiring Firebase Admin SDK will not work.")


# Global __app_id variable from the Canvas environment
# This will be used to construct Supabase paths according to the specified structure.
# For local development, it defaults to 'default-app-id'.
app_id = os.environ.get('__app_id', 'default-app-id')
print(f"Using app_id: {app_id}")


# --- Google Cloud Speech-to-Text Configuration ---
# SpeechClient will automatically use GOOGLE_APPLICATION_CREDENTIALS if set,
# or the default credentials configured for the environment (e.g., via gcloud auth application-default login)
# Ensure Speech-to-Text API is enabled in your Google Cloud Project.
speech_client = speech.SpeechClient()
print("Google Speech-to-Text client initialized.")


# --- Pydantic Models for API (Frontend) ---
class RecordCreate(BaseModel):
    # Model for creating new records (expense/income/KM)
    type_of_record: str # Ex: "Despesa", "Receita", "KM"
    category: str       # Ex: "Combustível", "Corrida", "Manutencao"
    value: float
    quantity: float | None = None # Optional quantity (for liters, etc.)
    observations: str | None = None # Optional observations

class ReminderCreate(BaseModel):
    # Model for creating new maintenance reminders
    maintenance_type: str # Ex: "Óleo", "Pneu", "Filtro"
    target_km: float


# --- Dependency to get current user ID for API endpoints (Frontend) ---
async def get_current_user_id_from_auth(request: Request) -> str:
    """
    Authenticates the user based on the Firebase ID token provided in the Authorization header
    and returns their Firebase UID.
    """
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        print("DEBUG: Authorization header missing for API request.")
        raise HTTPException(status_code=401, detail="Authorization header missing")

    token_parts = auth_header.split("Bearer ")
    if len(token_parts) != 2:
        print("DEBUG: Bearer token format invalid.")
        raise HTTPException(status_code=401, detail="Bearer token missing or malformed")

    token = token_parts[1]

    if not firebase_admin._apps:
        print("ERROR: Firebase Admin SDK not initialized. Cannot verify ID token.")
        raise HTTPException(status_code=500, detail="Server authentication not configured.")

    try:
        # Verify the Firebase ID token. This is the standard way to authenticate
        # users on the backend when they send a token from the frontend.
        decoded_token = auth.verify_id_token(token)
        uid = decoded_token['uid']
        print(f"DEBUG: Successfully authenticated user with UID: {uid}")
        return uid
    except Exception as e:
        print(f"ERROR: Firebase ID token verification failed: {e}")
        # Depending on the error, you might want more specific messages
        raise HTTPException(status_code=401, detail=f"Invalid authentication token: {e}")


# --- Common Helper for Twilio XML Response ---
def create_twiml_response(msg: str) -> Response:
    """Helper function to create a Twilio TwiML XML response."""
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message>{msg}</Message></Response>"""
    print(f"DEBUG: TwiML response being sent: \n{twiml}")
    return Response(content=twiml, media_type="application/xml")


# --- Root Endpoint for Health Check ---
@app.get("/")
async def read_root():
    """Basic health check endpoint."""
    return {"message": "Olá, Driverscash! Seu backend está funcionando e conectado ao Supabase."}


# --- Test Endpoint for Supabase Connection ---
@app.get("/test_supabase_connection")
async def test_supabase_connection():
    """Tests the Supabase connection by trying to fetch from a test table."""
    try:
        # Attempt to fetch from a non-existent table or a simple query
        # This checks if the client can communicate with the Supabase API.
        response = supabase.from_('dummy_table').select('*').limit(0).execute()
        print(f"DEBUG: Supabase test connection response: {response.status_code}")
        return {"status": "sucesso", "message": "Conexão Supabase bem-sucedida!", "data": response.data}
    except Exception as e:
        print(f"ERROR: Failed to connect to Supabase: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao conectar ao Supabase: {e}. Verifique as variáveis de ambiente e as credenciais.")


# --- WhatsApp Webhook Endpoint ---
@app.post("/whatsapp_webhook")
async def whatsapp_webhook(request: Request):
    """
    Handles incoming messages from WhatsApp via Twilio.
    Processes text and voice messages, and interacts with Supabase.
    """
    form_data = await request.form()

    from_number = form_data.get('From') # The sender's WhatsApp number (e.g., "whatsapp:+5511999999999")
    message_body = form_data.get('Body', '').strip() # The text message body
    num_media = int(form_data.get('NumMedia', 0)) # Number of media attachments

    # For WhatsApp, the user_id will be the 'From' number from Twilio.
    whatsapp_user_id = from_number

    print(f"\n--- Mensagem Recebida do WhatsApp ---")
    print(f"De: {whatsapp_user_id}")
    print(f"Mensagem Bruta: '{message_body}'")
    print(f"NumMedia: {num_media}")
    print(f"------------------------------------\n")

    # Define common help message content
    help_message_content = """Olá! 👋 Sou o Driverscash, seu assistente financeiro de viagem! 🚗💨
Comandos de Receita 💰
Faturamento Diario: Ex: Faturamento Diario 250.75
Receita: Ex: Receita Corrida 80
Faturamento Extra: Ex: Faturamento Extra Perfume 50

Comandos de Despesa ⛽
Abastecer: Ex: Abastecer 40 5.29 Gasolina 55000 (Litros ValorLitro TipoCombustivel KMAtual)
Despesa: Ex: Despesa Manutencao 100
Calcular Custo Combustivel: Ex: Calcular Custo Combustivel 100 10 6.50 (KM_Rodado Consumo_Medio Valor_Litro)

Outros Comandos Uteis 🛣️
KM: Ex: KM 12345
Calcular Consumo: Ex: Calcular Consumo 10000 10500 50 (KM_Inicial KM_Final Litros_Consumidos)
Relatorio KM: Ex: Relatorio KM
Lembrete Manutencao: Ex: Lembrete Oleo KM 10000
Lembrete Concluido: Ex: Lembrete Concluido Oleo
Meus Lembretes: Ex: Meus Lembretes

Relatorios Financeiros 📊
Relatorio Semana Financeiro: Ex: Relatorio Semana Financeiro
Relatorio Mes Financeiro: Ex: Relatorio Mes Financeiro
"""

    # --- Speech-to-Text Processing for Voice Messages ---
    if num_media > 0:
        media_url = form_data.get('MediaUrl0') # Twilio typically provides MediaUrl0 for the first media
        if media_url:
            print(f"DEBUG: Media detected. Media URL: {media_url}")
            try:
                audio_response = requests.get(media_url)
                audio_response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)

                audio_content = audio_response.content
                audio = speech.RecognitionAudio(content=audio_content)
                config = speech.RecognitionConfig(
                    encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS, # Common format for WhatsApp/Twilio
                    sample_rate_hertz=16000, # Typical sample rate for phone audio
                    language_code="pt-BR",   # Language code for Brazilian Portuguese
                    # enable_automatic_punctuation=True # Optional: for automatic punctuation
                )

                print("DEBUG: Sending audio to Speech-to-Text...")
                stt_response = speech_client.recognize(config=config, audio=audio)

                if stt_response.results:
                    # Get the first (most likely) transcription
                    transcribed_text = stt_response.results[0].alternatives[0].transcript
                    message_body = transcribed_text # Update message_body with transcribed text
                    print(f"DEBUG: Audio transcribed to: '{transcribed_text}'")
                else:
                    message_body = "" # No transcription obtained
                    print("DEBUG: No audio transcription was obtained.")

            except requests.exceptions.RequestException as req_err:
                response_message = f"Erro ao descarregar áudio: {req_err}"
                print(f"ERROR: {response_message}")
                return create_twiml_response(response_message)
            except Exception as stt_err:
                response_message = f"Erro na transcrição de áudio: {stt_err}. Verifique a ativação da API e credenciais do Google Cloud."
                print(f"ERROR: {response_message}")
                return create_twiml_response(response_message)
        else:
            print("DEBUG: NumMedia > 0 but MediaUrl0 not found.")
            response_message = "Mídia recebida, mas a URL do áudio não foi encontrada. Por favor, tente novamente ou use texto."
            return create_twiml_response(response_message)

    # Convert message body to lowercase for case-insensitive matching
    processed_message_body = message_body.lower()

    # Handle 'Oi' and 'Ajuda' commands
    if processed_message_body == 'oi':
        initial_greeting = "Olá! Como posso ajudar a lançar seus registros hoje?"
        combined_response = f"{initial_greeting}\n\n{help_message_content}"
        return create_twiml_response(combined_response)

    if processed_message_body == 'ajuda':
        return create_twiml_response(help_message_content)

    current_date = datetime.now().strftime("%Y-%m-%d")

    # Supabase table paths for records and reminders for this specific WhatsApp user
    # Following the pattern: artifacts/{appId}/users/{userId}/records (or reminders)
    records_table_path = f"artifacts/{app_id}/users/{whatsapp_user_id}/records"
    reminders_table_path = f"artifacts/{app_id}/users/{whatsapp_user_id}/reminders"

    try:
        # --- Process Expense Command ---
        match_despesa = re.match(r'despesa\s+(?:de\s+)?([a-zA-ZáéíóúÁÉÍÓÚãõÃÕçÇ\s]+)\s+([\d\.,]+)', processed_message_body, re.IGNORECASE)
        if match_despesa:
            expense_type = match_despesa.group(1).strip().capitalize()
            amount_str = match_despesa.group(2).replace(',', '.').strip()
            try:
                amount = float(amount_str)
            except ValueError:
                return create_twiml_response("Valor inválido para Despesa. Use 'Despesa Tipo Valor' com um número (ex: 150.50).")

            record_data = {
                "user_id": whatsapp_user_id, # Storing user_id explicitly in the record
                "origem": "WhatsApp",
                "data": current_date,
                "tipo_registro": "Despesa",
                "categoria": expense_type,
                "valor": amount,
                "quantidade": None,
                "observacoes": f"Registrado via WhatsApp por {whatsapp_user_id}"
            }
            supabase.from_(records_table_path).insert(record_data).execute()
            return create_twiml_response(f"Despesa de {expense_type} no valor de R${amount:.2f} registada com sucesso!")

        # --- Process Income Command ---
        match_receita = re.match(r'receita\s+(?:de\s+)?([a-zA-ZáéíóúÁÉÍÓÚãõÃÕçÇ\s]+)\s+([\d\.,]+)', processed_message_body, re.IGNORECASE)
        if match_receita:
            income_type = match_receita.group(1).strip().capitalize()
            amount_str = match_receita.group(2).replace(',', '.').strip()
            try:
                amount = float(amount_str)
            except ValueError:
                return create_twiml_response("Valor inválido para Receita. Use 'Receita Tipo Valor' com um número (ex: 80.00).")

            record_data = {
                "user_id": whatsapp_user_id,
                "origem": "WhatsApp",
                "data": current_date,
                "tipo_registro": "Receita",
                "categoria": income_type,
                "valor": amount,
                "quantidade": None,
                "observacoes": f"Registrado via WhatsApp por {whatsapp_user_id}"
            }
            supabase.from_(records_table_path).insert(record_data).execute()
            return create_twiml_response(f"Receita de {income_type} no valor de R${amount:.2f} registada com sucesso!")

        # --- Process Extra Income Command ---
        match_faturamento_extra = re.match(r'faturamento\s+extra\s+(?:de\s+)?([a-zA-ZáéíóúÁÉÍÓÚãõÃÕçÇ\s]+)\s+([\d\.,]+)', processed_message_body, re.IGNORECASE)
        if match_faturamento_extra:
            extra_type = match_faturamento_extra.group(1).strip().capitalize()
            amount_str = match_faturamento_extra.group(2).replace(',', '.').strip()
            try:
                amount = float(amount_str)
            except ValueError:
                return create_twiml_response("Valor inválido para Faturamento Extra. Use 'Faturamento Extra Tipo Valor' com um número (ex: 50.00).")

            record_data = {
                "user_id": whatsapp_user_id,
                "origem": "WhatsApp",
                "data": current_date,
                "tipo_registro": "Receita",
                "categoria": f"Extra: {extra_type}",
                "valor": amount,
                "quantidade": None,
                "observacoes": f"Registrado via WhatsApp por {whatsapp_user_id}"
            }
            supabase.from_(records_table_path).insert(record_data).execute()
            return create_twiml_response(f"Faturamento extra de {extra_type} no valor de R${amount:.2f} registado com sucesso!")

        # --- Process Daily Income Command ---
        match_faturamento_diario = re.match(r'(?:faturamento\s+diario|diario\s+faturamento)\s+([\d\.,]+)', processed_message_body, re.IGNORECASE)
        if match_faturamento_diario:
            amount_str = match_faturamento_diario.group(1).replace(',', '.').strip()
            try:
                amount = float(amount_str)
            except ValueError:
                return create_twiml_response("Valor inválido para Faturamento Diário. Use 'Faturamento Diario Valor' com um número (ex: 250.75).")

            record_data = {
                "user_id": whatsapp_user_id,
                "origem": "WhatsApp",
                "data": current_date,
                "tipo_registro": "Receita",
                "categoria": "Faturamento Diário",
                "valor": amount,
                "quantidade": None,
                "observacoes": f"Registrado via WhatsApp por {whatsapp_user_id}"
            }
            supabase.from_(records_table_path).insert(record_data).execute()
            return create_twiml_response(f"Faturamento diário de R${amount:.2f} registado com sucesso!")

        # --- Process Detailed Fueling Command ---
        # Abastecer 40 5.29 Gasolina 55000 (Litros ValorLitro TipoCombustivel KMAtual)
        match_abastecer = re.match(r'abastecer\s+([\d\.,]+)\s+([\d\.,]+)\s+(gasolina|etanol)\s+([\d\.,]+)', processed_message_body, re.IGNORECASE)
        if match_abastecer:
            litros_str = match_abastecer.group(1).replace(',', '.').strip()
            valor_litro_str = match_abastecer.group(2).replace(',', '.').strip()
            tipo_combustivel = match_abastecer.group(3).strip().capitalize()
            km_atual_str = match_abastecer.group(4).replace('.', '').replace(',', '.').strip() # Handle thousands separator

            try:
                litros = float(litros_str)
                valor_litro = float(valor_litro_str)
                km_atual = float(km_atual_str)
            except ValueError:
                return create_twiml_response("Formato inválido para Abastecer. Use 'Abastecer Litros ValorLitro TipoCombustivel KMAtual' (ex: Abastecer 40 5.29 Gasolina 55000).")

            custo_total = litros * valor_litro

            record_data = {
                "user_id": whatsapp_user_id,
                "origem": "WhatsApp",
                "data": current_date,
                "tipo_registro": "Despesa",
                "categoria": f"Combustível - {tipo_combustivel}",
                "valor": custo_total, # Total expense value
                "quantidade": litros,      # Quantity (Liters)
                "observacoes": f"KM Atual: {km_atual:.0f}, Valor Litro: R${valor_litro:.2f}. Registrado via WhatsApp por {whatsapp_user_id}"
            }
            supabase.from_(records_table_path).insert(record_data).execute()
            return create_twiml_response(f"Abastecimento de {litros:.2f} Litros de {tipo_combustivel} (R${custo_total:.2f}) registado. KM atual: {km_atual:.0f}.")

        # --- Process KM Command ---
        match_km = re.match(r'km\s+([\d\.,]+)', processed_message_body, re.IGNORECASE)
        if match_km:
            km_value_str = match_km.group(1).replace('.', '').replace(',', '.').strip() # Handle thousands separator
            try:
                km_value = float(km_value_str)
            except ValueError:
                return create_twiml_response("Valor inválido para KM. Use 'KM Valor' com um número (ex: 12345).")

            record_data = {
                "user_id": whatsapp_user_id,
                "origem": "WhatsApp",
                "data": current_date,
                "tipo_registro": "KM",
                "categoria": "Registro de KM",
                "valor": km_value, # KM value stored in 'valor' field
                "quantidade": None,
                "observacoes": f"Registrado via WhatsApp por {whatsapp_user_id}"
            }
            supabase.from_(records_table_path).insert(record_data).execute()

            response_message = f"Registro de KM {km_value:.2f} efetuado com sucesso!"

            # --- Maintenance Alert Logic (from Supabase) ---
            try:
                # Fetch all active reminders for this user
                reminders_response = supabase.from_(reminders_table_path).select('tipo_manutencao, km_alvo').eq('user_id', whatsapp_user_id).eq('status', 'Ativo').execute()
                all_reminders = reminders_response.data or [] # Ensure it's a list even if no data

                print(f"DEBUG: Maintenance Alert Logic - User: {whatsapp_user_id}, Current KM: {km_value}")
                print(f"DEBUG: Maintenance Alert Logic - Total active reminders fetched: {len(all_reminders)}")

                for reminder in all_reminders:
                    print(f"DEBUG: Maintenance Alert Logic - Processing reminder: {reminder}")

                    if isinstance(reminder.get('tipo_manutencao'), str) and isinstance(reminder.get('km_alvo'), (int, float)):
                        tipo_manutencao_alerta = reminder.get('tipo_manutencao')
                        target_km_reminder = float(reminder.get('km_alvo'))

                        print(f"DEBUG: Maintenance Alert Logic - Active reminder found: Type='{tipo_manutencao_alerta}', Target={target_km_reminder}")

                        ALERT_MARGIN = 500 # KM before target to send alert

                        if km_value >= (target_km_reminder - ALERT_MARGIN) and km_value < target_km_reminder:
                            alert_message = f"ALERTA! A manutenção de '{tipo_manutencao_alerta}' está a aproximar-se! Faltam aproximadamente {target_km_reminder - km_value:.0f} KM para o KM alvo de {target_km_reminder:.0f}."
                            response_message += f"\n\n{alert_message}"
                            print(f"MAINTENANCE ALERT TRIGGERED: {alert_message}")
                        elif km_value >= target_km_reminder:
                            alert_message = f"ALERTA! Você já passou do KM alvo de {target_km_reminder:.0f} para a manutenção de '{tipo_manutencao_alerta}'. Por favor, agende!"
                            response_message += f"\n\n{alert_message}"
                            print(f"MAINTENANCE ALERT TRIGGERED (PAST TARGET): {alert_message}")
                    else:
                        print(f"DEBUG: Maintenance Alert Logic - Reminder ignored (missing keys or incorrect types): {reminder}")
            except Exception as e_alert:
                print(f"ERROR: Failed to check maintenance reminders in Supabase: {e_alert}")
            # --- End of Maintenance Alert Logic ---
            return create_twiml_response(response_message)

        # --- Process Calculate Average Consumption Command ---
        # Calcular Consumo 10000 10500 50 (KM_Inicial KM_Final Litros_Consumidos)
        match_calcular_consumo = re.match(r'calcular\s+consumo\s+([\d\.,]+)\s+([\d\.,]+)\s+([\d\.,]+)', processed_message_body, re.IGNORECASE)
        if match_calcular_consumo:
            km_inicial_str = match_calcular_consumo.group(1).replace('.', '').replace(',', '.').strip()
            km_final_str = match_calcular_consumo.group(2).replace('.', '').replace(',', '.').strip()
            litros_consumidos_str = match_calcular_consumo.group(3).replace(',', '.').strip()
            try:
                km_inicial = float(km_inicial_str)
                km_final = float(km_final_str)
                litros_consumidos = float(litros_consumidos_str)
            except ValueError:
                return create_twiml_response("Formato inválido para Calcular Consumo. Use 'Calcular Consumo KM_Inicial KM_Final Litros_Consumidos' (ex: 10000 10500 50).")

            if litros_consumidos > 0:
                km_rodados = km_final - km_inicial
                consumo_medio = km_rodados / litros_consumidos
                response_message = f"Consumo médio calculado: {consumo_medio:.2f} KM/Litro."
            else:
                response_message = "Não é possível calcular o consumo médio com zero litros."
            return create_twiml_response(response_message)

        # --- Process Calculate Fuel Cost Command ---
        # Calcular Custo Combustivel 100 10 6.50 (KM_Rodado Consumo_Medio Valor_Litro)
        match_custo_combustivel = re.match(r'calcular\s+custo\s+combustivel\s+([\d\.,]+)\s+([\d\.,]+)\s+([\d\.,]+)', processed_message_body, re.IGNORECASE)
        if match_custo_combustivel:
            km_rodado_str = match_custo_combustivel.group(1).replace('.', '').replace(',', '.').strip()
            consumo_medio_str = match_custo_combustivel.group(2).replace(',', '.').strip()
            valor_litro_str = match_custo_combustivel.group(3).replace(',', '.').strip()

            try:
                km_rodado = float(km_rodado_str)
                consumo_medio = float(consumo_medio_str)
                valor_litro = float(valor_litro_str)
            except ValueError:
                return create_twiml_response("Formato inválido para Calcular Custo Combustivel. Use 'Calcular Custo Combustivel KM_Rodado Consumo_Medio Valor_Litro' (ex: 100 10 6.50).")

            if consumo_medio > 0:
                litros_necessarios = km_rodado / consumo_medio
                custo_estimado = litros_necessarios * valor_litro
                response_message = f"Custo estimado de combustível para {km_rodado:.0f} KM rodados (consumo {consumo_medio:.1f} KM/L, R${valor_litro:.2f}/L): R$ {custo_estimado:.2f}."
            else:
                response_message = "Consumo médio não pode ser zero para calcular o custo do combustível."
            return create_twiml_response(response_message)

        # --- Process KM Report Command ---
        match_relatorio_km_request = re.match(r'(?:relat[oó]rio\s+(?:de\s+)?km|km\s+relat[oó]rio)', processed_message_body, re.IGNORECASE)
        if match_relatorio_km_request:
            try:
                # Fetch KM records for this user
                records_response = supabase.from_(records_table_path).select('data, valor, tipo_registro').eq('user_id', whatsapp_user_id).eq('tipo_registro', 'KM').execute()
                km_records = records_response.data or []

                total_km_semana = 0
                total_km_mes = 0

                today = datetime.now().date()
                start_of_week = today - timedelta(days=today.weekday()) # Monday of current week
                start_of_month = today.replace(day=1) # First day of current month

                for record in km_records:
                    try:
                        record_date_str = record.get('data')
                        km_value_record = float(record.get('valor', 0))

                        record_date = datetime.strptime(record_date_str, "%Y-%m-%d").date()

                        if record_date >= start_of_week:
                            total_km_semana += km_value_record

                        if record_date >= start_of_month:
                            total_km_mes += km_value_record
                    except (ValueError, TypeError) as ve:
                        print(f"ERROR: Failed to process KM record (date/value format): {record} - {ve}")
                        continue

                response_message = f"Relatório de KM:\n"
                response_message += f"KM rodados esta semana: {total_km_semana:.2f}\n"
                response_message += f"KM rodados este mês: {total_km_mes:.2f}"
            except Exception as e_report:
                response_message = f"Erro ao gerar relatório de KM: {e_report}"
                print(f"ERROR: {response_message}")
            return create_twiml_response(response_message)

        # --- Process Maintenance Reminder Setup Command ---
        # Lembrete Oleo KM 10000
        match_lembrete_manutencao = re.match(r'lembrete\s+(?:de\s+)?([a-zA-ZáéíóúÁÉÍÓÚãõÃÕçÇ\s]+)\s+km\s+([\d\.,]+)', processed_message_body, re.IGNORECASE)
        if match_lembrete_manutencao:
            tipo_manutencao = match_lembrete_manutencao.group(1).strip().capitalize()
            target_km_str = match_lembrete_manutencao.group(2).replace('.', '').replace(',', '.').strip() # Handle thousands separator
            try:
                target_km = float(target_km_str)
            except ValueError:
                return create_twiml_response("Valor de KM alvo inválido para Lembrete. Use 'Lembrete Tipo KM Valor' (ex: Lembrete Oleo KM 10000).")

            reminder_data = {
                "user_id": whatsapp_user_id,
                "tipo_manutencao": tipo_manutencao,
                "km_alvo": target_km,
                "data_configuracao": current_date,
                "status": "Ativo"
            }
            supabase.from_(reminders_table_path).insert(reminder_data).execute()
            return create_twiml_response(f"Lembrete de manutenção para '{tipo_manutencao}' configurado para o KM {target_km:.0f}. Você será notificado!")

        # --- Process Mark Reminder as Completed Command ---
        # Lembrete Concluido Oleo or Oleo Lembrete Concluido
        tipo_manutencao_concluir = None
        match_concluido_patterns = [
            r'lembrete\s+concluido\s+([a-zA-ZáéíóúÁÉÍÓÚãõÃÕçÇ\s]+)',
            r'([a-zA-ZáéíóúÁÉÍÓÚãõÃÕçÇ\s]+)\s+lembrete\s+concluido',
            r'lembrete\s+de\s+([a-zA-ZáéíóúÁÉÍÓÚãõÃÕçÇ\s]+)\s+concluido',
            r'([a-zA-ZáéíóúÁÉÍÓÚãõÃÕçÇ\s]+)\s+de\s+lembrete\s+concluido'
        ]
        for pattern in match_concluido_patterns:
            match = re.match(pattern, processed_message_body, re.IGNORECASE)
            if match:
                tipo_manutencao_concluir = match.group(1).strip().capitalize()
                break

        if tipo_manutencao_concluir:
            # Update the status of the active reminder for this user
            # Supabase's update method can update rows matching a filter.
            update_response = supabase.from_(reminders_table_path).update({'status': 'Concluído'}).eq('user_id', whatsapp_user_id).eq('tipo_manutencao', tipo_manutencao_concluir).eq('status', 'Ativo').execute()

            if update_response.data and len(update_response.data) > 0:
                response_message = f"Lembrete de manutenção para '{tipo_manutencao_concluir}' marcado como concluído!"
            else:
                response_message = f"Nenhum lembrete de manutenção ativo para '{tipo_manutencao_concluir}' encontrado para marcar como concluído."
            return create_twiml_response(response_message)

        # --- Process List My Reminders Command ---
        # Meus Lembretes
        match_meus_lembretes = re.match(r'(?:meus\s+lembretes|lembretes\s+ativos|listar\s+lembretes)', processed_message_body, re_IGNORECASE)
        if match_meus_lembretes:
            reminders_response = supabase.from_(reminders_table_path).select('tipo_manutencao, km_alvo').eq('user_id', whatsapp_user_id).eq('status', 'Ativo').execute()
            active_reminders = reminders_response.data or []

            if active_reminders:
                response_message = "Seus Lembretes de Manutenção Ativos:\n"
                for reminder in active_reminders:
                    response_message += f"- {reminder.get('tipo_manutencao')}: KM Alvo {reminder.get('km_alvo'):.0f}\n"
            else:
                response_message = "Você não tem lembretes de manutenção ativos no momento."
            return create_twiml_response(response_message)

        # --- Process Weekly Financial Report Command ---
        # Relatorio Semana Financeiro
        match_relatorio_semanal_financeiro = re.match(r'relat[oó]rio\s+(?:semana\s+financeiro|financeiro\s+semanal)', processed_message_body, re_IGNORECASE)
        if match_relatorio_semanal_financeiro:
            records_response = supabase.from_(records_table_path).select('data, tipo_registro, valor, categoria').eq('user_id', whatsapp_user_id).execute()
            all_records = records_response.data or []

            total_ganhos_semana = 0
            total_despesas_semana = 0
            despesas_por_categoria_semana = {}

            today = datetime.now().date()
            start_of_week = today - timedelta(days=today.weekday()) # Monday of current week
            end_of_week = start_of_week + timedelta(days=6) # Sunday of current week

            for record in all_records:
                try:
                    record_date_str = record.get('data')
                    record_type = record.get('tipo_registro')
                    record_value = float(str(record.get('valor', '0')))
                    record_category = record.get('categoria', 'Outros')

                    if record_date_str and record_type and record_value is not None:
                        record_date = datetime.strptime(record_date_str, "%Y-%m-%d").date()

                        if start_of_week <= record_date <= end_of_week:
                            if record_type.strip().lower() == 'receita':
                                total_ganhos_semana += record_value
                            elif record_type.strip().lower() == 'despesa':
                                total_despesas_semana += record_value
                                despesas_por_categoria_semana[record_category] = despesas_por_categoria_semana.get(record_category, 0.0) + record_value
                except (ValueError, TypeError) as ve:
                    print(f"ERROR: Failed to process record for weekly report (date/value/type format): {record} - {ve}")
                    continue

            lucro_semana = total_ganhos_semana - total_despesas_semana

            report_parts = [
                f"📊 Relatório Semanal ({start_of_week.strftime('%d/%m')} - {end_of_week.strftime('%d/%m')}) ",
                f"Ganhos Totais: R$ {total_ganhos_semana:.2f}",
                f"Despesas Totais: R$ {total_despesas_semana:.2f}",
                f"Lucro: R$ {lucro_semana:.2f}",
                "\n*Detalhes das Despesas por Categoria:*"
            ]

            if despesas_por_categoria_semana:
                for category, amount in despesas_por_categoria_semana.items():
                    report_parts.append(f"- {category}: R$ {amount:.2f}")
            else:
                report_parts.append("- Nenhuma despesa registada esta semana.")

            response_message = "\n".join(report_parts)
            return create_twiml_response(response_message)

        # --- Process Monthly Financial Report Command ---
        # Relatorio Mes Financeiro
        match_relatorio_mensal_financeiro = re.match(r'relat[oó]rio\s+(?:m[eê]s\s+financeiro|financeiro\s+mensal)', processed_message_body, re_IGNORECASE)
        if match_relatorio_mensal_financeiro:
            records_response = supabase.from_(records_table_path).select('data, tipo_registro, valor, categoria').eq('user_id', whatsapp_user_id).execute()
            all_records = records_response.data or []

            total_ganhos_mes = 0
            total_despesas_mes = 0
            despesas_por_categoria_mes = {}

            today = datetime.now().date()
            start_of_month = today.replace(day=1) # First day of current month

            meses_pt = [
                "Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
                "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"
            ]

            for record in all_records:
                try:
                    record_date_str = record.get('data')
                    record_type = record.get('tipo_registro')
                    record_value = float(str(record.get('valor', '0')))
                    record_category = record.get('categoria', 'Outros')

                    if record_date_str and record_type and record_value is not None:
                        record_date = datetime.strptime(record_date_str, "%Y-%m-%d").date()

                        if record_date >= start_of_month and record_date.month == today.month and record_date.year == today.year:
                            if record_type.strip().lower() == 'receita':
                                total_ganhos_mes += record_value
                            elif record_type.strip().lower() == 'despesa':
                                total_despesas_mes += record_value
                                despesas_por_categoria_mes[record_category] = despesas_por_categoria_mes.get(record_category, 0.0) + record_value
                except (ValueError, TypeError) as ve:
                    print(f"ERROR: Failed to process record for monthly report (date/value/type format): {record} - {ve}")
                    continue

            lucro_mes = total_ganhos_mes - total_despesas_mes

            report_parts = [
                f"📊 Relatório Mensal ({meses_pt[start_of_month.month - 1]}/{start_of_month.year}) 📊",
                f"Ganhos Totais: R$ {total_ganhos_mes:.2f}",
                f"Despesas Totais: R$ {total_despesas_mes:.2f}",
                f"Lucro: R$ {lucro_mes:.2f}",
                "\n*Detalhes das Despesas por Categoria:*"
            ]

            if despesas_por_categoria_mes:
                for category, amount in despesas_por_categoria_mes.items():
                    report_parts.append(f"- {category}: R$ {amount:.2f}")
            else:
                report_parts.append("- Nenhuma despesa registada este mês.")

            response_message = "\n".join(report_parts)
            return create_twiml_response(response_message)

        # If no command matched
        response_message = "Não consegui entender o comando. Por favor, verifique a mensagem de ajuda e tente novamente.\n\n" + help_message_content
        return create_twiml_response(response_message)

    except Exception as e:
        response_message = f"Erro inesperado ao processar sua mensagem: {e}"
        print(f"ERROR: General webhook error: {e}")
        return create_twiml_response(response_message)


# --- Frontend API Endpoints (requiring authentication) ---

# Endpoint to get all records for the authenticated user
@app.get("/api/records")
async def get_all_records_api(user_id: str = Depends(get_current_user_id_from_auth)):
    """
    Retrieves all records (expenses, incomes, KM) for the authenticated user from Supabase.
    """
    records_table_path = f"artifacts/{app_id}/users/{user_id}/records"
    try:
        # Fetch records filtered by user_id to ensure data isolation.
        # Supabase client will ensure only data for this specific user_id in this path is returned.
        response = supabase.from_(records_table_path).select('*').eq('user_id', user_id).execute()
        return {"status": "sucesso", "data": response.data}
    except Exception as e:
        print(f"ERROR: Failed to fetch records from Supabase via API for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao obter registros: {e}")

# Endpoint to add a new record via the frontend API
@app.post("/api/records")
async def add_record_api(record: RecordCreate, user_id: str = Depends(get_current_user_id_from_auth)):
    """
    Adds a new record (expense, income, or KM) for the authenticated user to Supabase.
    """
    records_table_path = f"artifacts/{app_id}/users/{user_id}/records"
    current_date = datetime.now().strftime("%Y-%m-%d")

    record_data = {
        "user_id": user_id, # Ensure user_id is stored with the record for RLS/filtering
        "origem": "API Web",
        "data": current_date,
        "tipo_registro": record.type_of_record.capitalize(),
        "categoria": record.category.capitalize(),
        "valor": record.value,
        "quantidade": record.quantity if record.quantity is not None else None,
        "observacoes": record.observations if record.observations else "Registrado via API Web"
    }

    try:
        supabase.from_(records_table_path).insert(record_data).execute()
        return {"status": "sucesso", "message": "Registro adicionado com sucesso via API web!"}
    except Exception as e:
        print(f"ERROR: Failed to add record to Supabase via API for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao adicionar registro via API: {e}")

# Endpoint to get all maintenance reminders for the authenticated user
@app.get("/api/reminders")
async def get_all_reminders_api(user_id: str = Depends(get_current_user_id_from_auth)):
    """
    Retrieves all maintenance reminders for the authenticated user from Supabase.
    """
    reminders_table_path = f"artifacts/{app_id}/users/{user_id}/reminders"
    try:
        response = supabase.from_(reminders_table_path).select('*').eq('user_id', user_id).execute()
        return {"status": "sucesso", "data": response.data}
    except Exception as e:
        print(f"ERROR: Failed to fetch reminders from Supabase via API for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao obter lembretes: {e}")

# Endpoint to add a new maintenance reminder via the frontend API
@app.post("/api/reminders")
async def add_reminder_api(reminder: ReminderCreate, user_id: str = Depends(get_current_user_id_from_auth)):
    """
    Adds a new maintenance reminder for the authenticated user to Supabase.
    """
    reminders_table_path = f"artifacts/{app_id}/users/{user_id}/reminders"
    current_date = datetime.now().strftime("%Y-%m-%d")

    reminder_data = {
        "user_id": user_id, # Ensure user_id is stored with the reminder
        "tipo_manutencao": reminder.maintenance_type.capitalize(),
        "km_alvo": reminder.target_km,
        "data_configuracao": current_date,
        "status": "Ativo"
    }

    try:
        supabase.from_(reminders_table_path).insert(reminder_data).execute()
        return {"status": "sucesso", "message": "Lembrete adicionado com sucesso via API web!"}
    except Exception as e:
        print(f"ERROR: Failed to add reminder to Supabase via API for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao adicionar lembrete via API:{e}")
     