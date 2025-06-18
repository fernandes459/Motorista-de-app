import os
import re
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Response
from twilio.twiml.messaging_response import MessagingResponse
from supabase import create_client, Client
import json

# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

# Configuração do Supabase
# Certifique-se de que SUPABASE_URL e SUPABASE_KEY estão definidos no Render Environment Variables
# ou no seu arquivo .env localmente.
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    # Se as variáveis de ambiente não estiverem configuradas, o aplicativo não deve iniciar.
    # Em um ambiente de produção como o Render, elas devem ser definidas lá.
    print("ERRO: Variáveis de ambiente SUPABASE_URL ou SUPABASE_KEY não configuradas.")
    exit(1) # Sair do aplicativo se as variáveis essenciais estiverem faltando

# Inicializa o cliente Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI()

# Função auxiliar para enviar mensagens via WhatsApp
def send_whatsapp_message(to_number: str, message_body: str) -> Response:
    """
    Envia uma mensagem de texto para um número de WhatsApp usando Twilio.
    Esta função não é diretamente usada na rota de webhook, mas é um exemplo
    de como enviar mensagens de volta para o usuário se necessário em outras partes do código.
    """
    # Exemplo (comentado porque a resposta é feita via TwiML na rota de webhook)
    # from twilio.rest import Client as TwilioClient
    # account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    # auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    # twilio_client = TwilioClient(account_sid, auth_token)
    # twilio_client.messages.create(
    #     from_='whatsapp:+14155238886', # Seu número Twilio WhatsApp
    #     to=f'whatsapp:{to_number}',
    #     body=message_body
    # )
    # A resposta para o webhook é via TwiML, então esta função é mais para envios proativos.
    return Response(content=f"<Response><Message>{message_body}</Message></Response>", media_type="application/xml")

# Função para obter ou criar uma categoria padrão
async def get_or_create_default_category_id():
    """
    Busca o ID da categoria 'Outros' ou a cria se não existir.
    """
    print("Função get_or_create_default_category_id iniciada.")
    try:
        # Tenta encontrar a categoria 'Outros'
        response = supabase.from_('categorias').select('id').eq('nome', 'Outros').execute()
        print(f"Resposta Supabase ao buscar categoria 'Outros': {response.data}")

        if response.data and len(response.data) > 0:
            category_id = response.data[0]['id']
            print(f"Categoria 'Outros' encontrada. ID: {category_id}")
            return category_id
        else:
            # Se 'Outros' não existir, cria a categoria
            print("Categoria 'Outros' não encontrada, criando...")
            insert_response = supabase.from_('categorias').insert({"nome": "Outros"}).execute()
            print(f"Resposta Supabase ao criar categoria 'Outros': {insert_response.data}")

            if insert_response.data and len(insert_response.data) > 0:
                category_id = insert_response.data[0]['id']
                print(f"Categoria 'Outros' criada com sucesso. ID: {category_id}")
                return category_id
            else:
                print("ERRO: Falha ao criar a categoria 'Outros'.")
                return None
    except Exception as e:
        print(f"ERRO ao buscar ou criar categoria padrão: {e}")
        return None

# Função para obter o ID do usuário ou criá-lo
async def get_or_create_user_id(from_number: str):
    """
    Busca o ID do usuário pelo número de telefone ou o cria se não existir.
    """
    print(f"Função get_or_create_user_id iniciada para o número: {from_number}")
    try:
        # Tenta encontrar o usuário
        response = supabase.from_('users').select('id').eq('telefone', from_number).execute()
        print(f"Resposta Supabase ao buscar usuário {from_number}: {response.data}")

        if response.data and len(response.data) > 0:
            user_id = response.data[0]['id']
            print(f"Usuário {from_number} encontrado. ID: {user_id}")
            return user_id
        else:
            # Se o usuário não existir, cria
            print(f"Usuário {from_number} não encontrado, criando...")
            insert_response = supabase.from_('users').insert({"telefone": from_number}).execute()
            print(f"Resposta Supabase ao criar usuário {from_number}: {insert_response.data}")

            if insert_response.data and len(insert_response.data) > 0:
                user_id = insert_response.data[0]['id']
                print(f"Usuário {from_number} criado com sucesso. ID: {user_id}")
                return user_id
            else:
                print(f"ERRO: Falha ao criar o usuário {from_number}.")
                return None
    except Exception as e:
        print(f"ERRO ao buscar ou criar usuário: {e}")
        return None

@app.post("/webhook")
async def handle_whatsapp_webhook(Body: str = Form(...), From: str = Form(...)):
    """
    Endpoint para receber mensagens do WhatsApp via webhook da Twilio.
    Processa a mensagem, extrai informações e registra a transação no Supabase.
    """
    print(f"Webhook recebido de {From} com mensagem: {Body}")
    response_message = "Desculpe, não entendi. Por favor, use o formato 'GASTO [valor] [local]'. Ex: 'GASTO 50.00 POSTO'."

    twiml_response = MessagingResponse()

    # Tenta obter o user_id (ou criá-lo)
    user_id = await get_or_create_user_id(From)
    if not user_id:
        response_message = "Não foi possível identificar ou registrar seu usuário. Tente novamente."
        twiml_response.message(response_message)
        return Response(content=str(twiml_response), media_type="application/xml")

    # Padrão para "GASTO [valor] [local]"
    match = re.match(r"GASTO\s+([\d.,]+)\s+(.+)", Body.upper())

    if match:
        valor_str = match.group(1).replace(',', '.') # Substitui vírgula por ponto para float
        local = match.group(2).strip()
        
        try:
            valor = float(valor_str)
            
            # Obtém ou cria a categoria padrão 'Outros'
            categoria_id = await get_or_create_default_category_id()
            
            if categoria_id is None:
                response_message = "Não foi possível registrar a categoria padrão. Tente novamente."
                twiml_response.message(response_message)
                return Response(content=str(twiml_response), media_type="application/xml")

            # Inserir no Supabase
            data_to_insert = {
                "valor": valor,
                "local": local,
                "data_transacao": datetime.now().isoformat(), # Formato ISO 8601
                "user_id": user_id,
                "categoria_id": categoria_id # Usando o ID da categoria 'Outros'
            }
            print(f"Dados a serem inseridos: {data_to_insert}")

            insert_response = supabase.from_('transacoes').insert(data_to_insert).execute()
            print(f"Resposta de inserção do Supabase: {insert_response.data}, Erro: {insert_response.error}")

            if insert_response.data:
                response_message = f"Gasto de R${valor:.2f} em {local} registrado com sucesso! 🎉"
            else:
                error_detail = insert_response.error.message if insert_response.error else "Erro desconhecido."
                response_message = f"Ocorreu um erro ao registrar o gasto: {error_detail}"
                print(f"ERRO na inserção do Supabase: {error_detail}")

        except ValueError:
            response_message = "Valor inválido. Por favor, insira um número. Ex: 'GASTO 50.00 POSTO'."
        except Exception as e:
            response_message = f"Ocorreu um erro inesperado: {e}"
            print(f"ERRO inesperado no webhook: {e}")
    else:
        # Se não corresponder ao padrão, tenta outros comandos ou informa o formato correto.
        if Body.upper() == "OLÁ":
            response_message = "Olá! 👋 Eu sou seu assistente de controle de gastos. Para registrar um gasto, use o formato 'GASTO [valor] [local]'. Ex: 'GASTO 50.00 POSTO'."
        elif Body.upper() == "AJUDA":
            response_message = "Para registrar um gasto: 'GASTO [valor] [local]'. Ex: 'GASTO 50.00 POSTO'. Em breve, terei mais funcionalidades!"
        # Adicione mais comandos ou lógica aqui conforme necessário
    
    twiml_response.message(response_message)
    return Response(content=str(twiml_response), media_type="application/xml")

@app.get("/")
async def root():
    """
    Endpoint raiz para verificar se o aplicativo está funcionando.
    """
    return {"message": "Bem-vindo ao Motorista de App! O webhook está em /webhook"}
