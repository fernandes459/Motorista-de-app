import os
import re
from datetime import datetime, timedelta
from pydantic import BaseModel
from fastapi import FastAPI, Request, Response, HTTPException, Depends
import requests
from google.cloud import speech # Pode não estar instalado
import io
import firebase_admin
from firebase_admin import credentials, auth
from supabase import create_client, Client
import json

print("DEBUG: Script main.py iniciado.") # LINHA 1

app = FastAPI()
print("DEBUG: FastAPI app inicializado.") # LINHA 2

# --- Supabase Configuration ---
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("DEBUG: SUPABASE_URL ou SUPABASE_KEY nao configurados. Levantar RuntimeError.") # LINHA 3
    raise RuntimeError("SUPABASE_URL and SUPABASE_KEY environment variables must be set.")

try:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    print("DEBUG: Supabase client inicializado.") # LINHA 4
except Exception as e:
    print(f"DEBUG: ERRO ao inicializar Supabase client: {e}") # LINHA 5
    raise RuntimeError(f"Failed to initialize Supabase client: {e}")


# --- Firebase Admin SDK Initialization ---
FIREBASE_CREDENTIALS_JSON_STR = os.environ.get("FIREBASE_CREDENTIALS_JSON")

if FIREBASE_CREDENTIALS_JSON_STR:
    try:
        cred_dict = json.loads(FIREBASE_CREDENTIALS_JSON_STR)
        cred = credentials.Certificate(cred_dict)
        if not firebase_admin._apps:
            firebase_admin.initialize_app(cred)
        print("DEBUG: Firebase Admin SDK inicializado com sucesso.") # LINHA 6
    except Exception as e:
        print(f"DEBUG: ERRO ao inicializar Firebase Admin SDK: {e}") # LINHA 7
        # COMENTE OU REMOVA ESTA LINHA SE AINDA NÃO FEZ:
        # raise RuntimeError(f"Failed to initialize Firebase Admin SDK: {e}")
        print("DEBUG: Firebase Admin SDK nao inicializado devido a erro, mas o app vai continuar se possivel.") # LINHA 8
else:
    print("DEBUG: FIREBASE_CREDENTIALS_JSON nao configurado. Firebase Admin SDK nao inicializado.") # LINHA 9


# Global __app_id variable from the Canvas environment
app_id = os.environ.get('__app_id', 'default-app-id')
print(f"DEBUG: Usando app_id: {app_id}") # LINHA 10

# --- Google Cloud Speech-to-Text Configuration ---
# COMENTE TEMPORARIAMENTE SE NAO TIVER GOOGLE_APPLICATION_CREDENTIALS OU SE NAO USA SPEECH-TO-TEXT
# speech_client = speech.SpeechClient()
# print("DEBUG: Google Speech-to-Text client inicializado.") # LINHA 11


# ... (o resto do seu código permanece igual) ...

# Final do arquivo (antes de qualquer endpoint, etc.)
print("DEBUG: Fim da inicialização principal do script.") # LINHA 12