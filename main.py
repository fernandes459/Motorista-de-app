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
        print(f"Error initializing Firebase Admin SDK: {e}")
        # Depending on criticality, you might want to raise an exception or just log
        # For this example, we'll allow the app to start but log the error
        raise RuntimeError(f"Failed to initialize Firebase Admin SDK: {e}")
else:
    print("FIREBASE_CREDENTIALS_JSON environment variable not set. Firebase Admin SDK not initialized.")

# --- Google Cloud Speech-to-Text Configuration (Optional, if you're using it) ---
# Ensure GOOGLE_APPLICATION_CREDENTIALS environment variable is set on Render
# if you are using file-based authentication.
# For direct JSON content, consider loading similarly to Firebase if not using default GKE/Compute Engine roles.

# --- Pydantic Models for Request Body Validation ---
class Message(BaseModel):
    To: str
    From: str
    Body: str = None # Mensagem de texto pode ser opcional se for áudio
    MediaUrl0: str = None # URL do áudio, se for uma mensagem de áudio

class ReminderCreate(BaseModel):
    maintenance_type: str
    target_km: int

class UserLogin(BaseModel):
    token: str # Firebase ID Token

# --- Dependency to get authenticated user ID from Firebase ID Token ---
async def get_current_user_id_from_auth(request: Request) -> str:
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Authorization header missing")
    
    token = auth_header.split("Bearer ")[1] if "Bearer " in auth_header else auth_header
    
    try:
        decoded_token = auth.verify_id_token(token)
        uid = decoded_token['uid']
        return uid
    except Exception as e:
        raise HTTPException(status_code=401, detail=f"Invalid or expired token: {e}")

# --- NEW: Function to get or create a user in Supabase's 'usuarios' table ---
async def get_or_create_user(whatsapp_number: str):
    """
    Checks if a user exists in the 'usuarios' table by their whatsapp_number.
    If not, creates a new user entry. Returns the user's Supabase ID (UID).
    """
    user_identifier = whatsapp_number 

    print(f"Função get_or_create_user iniciada para o número: {user_identifier}")

    try:
        # 1. Tentar encontrar o usuário existente
        # Ajuste 'usuarios' para o nome exato da sua tabela se for diferente (ex: 'users')
        # Ajuste 'numero_do_whatsapp' para o nome exato da sua coluna na tabela de usuários
        response = supabase.from_('usuarios').select('*').eq('número_do_whatsapp', user_identifier).limit(1).execute()
        user_data = response.data

        if user_data:
            print(f"Usuário existente encontrado no Supabase: {user_data[0]}")
            # Supondo que você queira retornar algum ID único do Supabase,
            # como o 'id' da linha criada automaticamente pelo Supabase.
            return user_data[0].get('id') # Retorna o ID da linha existente
        else:
            # 2. Se o usuário não existir, crie um novo
            print(f"Usuário não encontrado. Criando novo usuário para: {user_identifier}")
            new_user_data = {
                "número_do_whatsapp": user_identifier,
                # Adicione aqui quaisquer outros campos obrigatórios para sua tabela 'usuarios'
                # Por exemplo: "nome": "Novo Usuário", "email": "email_gerado@exemplo.com"
            }
            insert_response = supabase.from_('usuarios').insert(new_user_data).execute()
            
            if insert_response.data:
                print(f"Novo usuário criado no Supabase: {insert_response.data[0]}")
                return insert_response.data[0].get('id') # Retorna o ID da nova linha
            else:
                print(f"ERRO: Nenhuma dado retornado após tentar criar usuário: {insert_response.data}")
                raise HTTPException(status_code=500, detail="Erro ao criar novo usuário no Supabase: Nenhuma dado retornado")

    except Exception as e:
        print(f"ERROR: Erro ao buscar ou criar usuário no Supabase: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao buscar ou criar usuário: {e}")


# --- Helper for sending WhatsApp messages via Twilio ---
def send_whatsapp_message(to: str, body: str):
    account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
    auth_token = os.environ.get("TWILIO_AUTH_TOKEN")
    twilio_whatsapp_number = os.environ.get("TWILIO_WHATSAPP_NUMBER")

    if not account_sid or not auth_token or not twilio_whatsapp_number:
        print("TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, or TWILIO_WHATSAPP_NUMBER not set.")
        return

    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "To": to,
        "From": twilio_whatsapp_number,
        "Body": body
    }

    try:
        response = requests.post(url, headers=headers, data=data, auth=(account_sid, auth_token))
        response.raise_for_status() # Raise an HTTPError for bad responses (4xx or 5xx)
        print(f"WhatsApp message sent successfully to {to}: {response.json()}")
    except requests.exceptions.RequestException as e:
        print(f"Error sending WhatsApp message to {to}: {e}")

# --- Google Cloud Speech-to-Text Function ---
def transcribe_audio_from_url(audio_url: str):
    client = speech.SpeechClient() # Uses GOOGLE_APPLICATION_CREDENTIALS by default
    
    try:
        response = requests.get(audio_url, stream=True)
        response.raise_for_status() # Raise an exception for HTTP errors

        audio_content = response.content

        audio = speech.RecognitionAudio(content=audio_content)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.OGG_OPUS, # Common for WhatsApp
            sample_rate_hertz=16000, # Adjust if you know the sample rate
            language_code="pt-BR",
            model="default" # Or "enhanced" for better accuracy, if enabled
        )

        operation = client.long_running_recognize(config=config, audio=audio)
        print("Waiting for audio transcription to complete...")
        response = operation.result(timeout=300) # Wait for up to 5 minutes

        transcripts = [result.alternatives[0].transcript for result in response.results]
        return " ".join(transcripts) if transcripts else ""

    except Exception as e:
        print(f"Error during audio transcription: {e}")
        return ""

# --- WhatsApp Webhook Endpoint ---
@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    form_data = await request.form()
    message = Message(**form_data)

    from_number = message.From # e.g., "whatsapp:+5511999999999"
    message_body = message.Body
    media_url = message.MediaUrl0

    print(f"Received message from: {from_number}")
    print(f"Message body: {message_body}")
    print(f"Media URL: {media_url}")

    user_id = None
    try:
        # Get or create user and get their Supabase ID
        supabase_user_id = await get_or_create_user(from_number)
        user_id = supabase_user_id # Store the user_id for later use if needed

        # Fetch user's current vehicle information (assuming a 'veiculos' table)
        # This is a placeholder, adapt to your actual vehicle data structure
        vehicle_response = supabase.from_('veiculos').select('*').eq('user_id', user_id).limit(1).execute()
        vehicle_data = vehicle_response.data
        user_current_km = vehicle_data[0]['km_atual'] if vehicle_data else None

        # Fetch active reminders for the user
        reminders_table_path = f"artifacts/{app_id}/users/{user_id}/reminders"
        reminders_response = supabase.from_(reminders_table_path).select('*').eq('status', 'Ativo').execute()
        active_reminders = reminders_response.data

        response_message = "Olá! Sou seu assistente de manutenção veicular. "

        if media_url:
            transcribed_text = transcribe_audio_from_url(media_url)
            print(f"Transcribed audio: {transcribed_text}")
            message_body = transcribed_text # Use transcribed text as message body

        if message_body:
            lower_body = message_body.lower()

            # --- Logic to handle different commands ---
            if "olá" in lower_body or "oi" in lower_body or "iniciar" in lower_body:
                response_message += "Como posso ajudar você hoje?"
                response_message += "\n* Opções disponíveis:"
                response_message += "\n* 'Adicionar Lembrete': Para adicionar uma nova manutenção."
                response_message += "\n* 'Lembretes': Para ver suas manutenções agendadas."
                response_message += "\n* 'Atualizar KM': Para informar a quilometragem atual do seu veículo."
                response_message += "\n* 'Ajuda': Para ver este menu novamente."

            elif "adicionar lembrete" in lower_body:
                response_message = "Certo! Qual o tipo de manutenção (Ex: 'Troca de óleo', 'Revisão geral') e qual a KM alvo para ela (Ex: '20000')? Por favor, informe no formato: Tipo: [tipo], KM: [km_alvo]."
                # Here you might set a state for the user to expect next input for reminder details
            
            elif "tipo:" in lower_body and "km:" in lower_body:
                try:
                    match_type = re.search(r"tipo:\s*([^,]+)", lower_body)
                    match_km = re.search(r"km:\s*(\d+)", lower_body)

                    if match_type and match_km:
                        maintenance_type = match_type.group(1).strip()
                        target_km = int(match_km.group(1).strip())

                        # Add the reminder to Supabase
                        current_date = datetime.now().strftime("%Y-%m-%d")
                        reminder_data = {
                            "user_id": user_id,
                            "tipo_manutencao": maintenance_type.capitalize(),
                            "km_alvo": target_km,
                            "data_configuracao": current_date,
                            "status": "Ativo"
                        }
                        supabase.from_(reminders_table_path).insert(reminder_data).execute()
                        response_message = f"Lembrete de '{maintenance_type}' para {target_km} KM adicionado com sucesso!"
                    else:
                        response_message = "Formato inválido para adicionar lembrete. Use: Tipo: [tipo], KM: [km_alvo]."
                except ValueError:
                    response_message = "Formato de KM inválido. Use um número inteiro."
                except Exception as e:
                    response_message = f"Erro ao adicionar lembrete: {e}"

            elif "lembretes" in lower_body:
                if active_reminders:
                    response_message = "Seus lembretes de manutenção ativos:\n"
                    for r in active_reminders:
                        response_message += f"- {r.get('tipo_manutencao')} (KM Alvo: {r.get('km_alvo')})\n"
                else:
                    response_message = "Você não tem lembretes de manutenção ativos no momento."
            
            elif "atualizar km" in lower_body:
                response_message = "Por favor, informe a quilometragem atual do seu veículo no formato: KM Atual: [seu_km]."
                # Here you might set a state for the user to expect next input for KM update

            elif "km atual:" in lower_body:
                try:
                    match_km_atual = re.search(r"km atual:\s*(\d+)", lower_body)
                    if match_km_atual:
                        new_km = int(match_km_atual.group(1).strip())
                        # Update the user's current KM in Supabase (assuming 'veiculos' table)
                        # This is a placeholder, adapt to your actual vehicle data structure
                        if vehicle_data:
                            supabase.from_('veiculos').update({'km_atual': new_km}).eq('user_id', user_id).execute()
                            response_message = f"Sua quilometragem foi atualizada para {new_km} KM."
                            # Now, check for overdue reminders
                            overdue_reminders = []
                            for r in active_reminders:
                                if new_km >= r.get('km_alvo'):
                                    overdue_reminders.append(r.get('tipo_manutencao'))
                                    # Optionally, update reminder status to 'Concluído' or 'Vencido'
                                    supabase.from_(reminders_table_path).update({'status': 'Vencido'}).eq('id', r.get('id')).execute()
                            
                            if overdue_reminders:
                                response_message += "\n*Atenção*: As seguintes manutenções estão vencidas ou próximas:"
                                for reminder_type in overdue_reminders:
                                    response_message += f"\n- {reminder_type}"
                        else:
                            response_message = "Não encontramos informações de veículo para o seu usuário. Por favor, adicione seu veículo primeiro."
                    else:
                        response_message = "Formato inválido para KM Atual. Use: KM Atual: [seu_km]."
                except ValueError:
                    response_message = "Formato de KM inválido. Use um número inteiro."
                except Exception as e:
                    response_message = f"Erro ao atualizar KM: {e}"

            elif "ajuda" in lower_body:
                response_message += "Aqui estão as opções novamente:"
                response_message += "\n* 'Adicionar Lembrete': Para adicionar uma nova manutenção."
                response_message += "\n* 'Lembretes': Para ver suas manutenções agendadas."
                response_message += "\n* 'Atualizar KM': Para informar a quilometragem atual do seu veículo."
                response_message += "\n* 'Ajuda': Para ver este menu novamente."
            else:
                response_message = "Não entendi sua solicitação. Por favor, tente 'Ajuda' para ver as opções."
        else:
            response_message = "Não consegui processar sua mensagem. Por favor, envie uma mensagem de texto ou tente novamente."

    except HTTPException as e:
        response_message = f"Erro no serviço: {e.detail}"
        print(f"HTTPException in webhook: {e.detail}")
    except Exception as e:
        response_message = f"Ocorreu um erro inesperado: {e}"
        print(f"Unexpected error in webhook: {e}")

    send_whatsapp_message(from_number, response_message)
    return Response(content="<Response/>", media_type="text/xml")


# --- Endpoint for web frontend to get reminders ---
@app.get("/api/reminders")
async def get_reminders_api(user_id: str = Depends(get_current_user_id_from_auth)):
    """
    Fetches all reminders for the authenticated user from Supabase.
    """
    try:
        # Define the dynamic table path for user-specific reminders
        # Make sure 'app_id' is defined or passed correctly if used in path
        # Assuming 'app_id' is a global or derived variable, for this example
        # Let's assume app_id is a placeholder for a fixed string like 'drivercash_app' if used
        app_id = "drivercash_app" # Example placeholder if it's a fixed app ID
        
        reminders_table_path = f"artifacts/{app_id}/users/{user_id}/reminders"
        
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
    # Assuming 'app_id' is defined as above
    app_id = "drivercash_app" # Example placeholder if it's a fixed app ID

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
        return {"status": "sucesso", "message": "Lembrete adicionado com sucesso!"}
    except Exception as e:
        print(f"ERROR: Failed to add reminder to Supabase via API for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Erro ao adicionar lembrete via API: {e}")