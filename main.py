# Importações necessárias
from fastapi import FastAPI, Request, Response, Form # Importa Form para lidar com dados de formulário do Twilio
from supabase import create_client, Client # Importa Client para type hinting
from dotenv import load_dotenv
import os
from twilio.twiml.messaging_response import MessagingResponse # Importa para construir respostas TwiML (XML)
import datetime # Importa para trabalhar com datas (para o campo 'data' nas transacoes)
import traceback # Importa para obter o rasto de erro completo

# Carregar variáveis de ambiente do arquivo .env (para uso local)
# No Render, essas variáveis são fornecidas pelo painel, mas load_dotenv é bom para testes locais.
load_dotenv()

# Inicializa a aplicação FastAPI
app = FastAPI()

# Configurações do Supabase (obtidas das variáveis de ambiente)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Inicializa o cliente Supabase
# É crucial que SUPABASE_URL e SUPABASE_KEY estejam corretas e com valores completos.
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Rota de health check - para o Render saber que a API está funcionando
@app.get("/")
async def root():
    return {"message": "API Driverscash está funcionando!"}

# Rota para receber as mensagens do WhatsApp (Webhook)
@app.post("/webhook")
async def whatsapp_webhook(Body: str = Form(...), From: str = Form(...)):
    # Inicializa um objeto MessagingResponse para construir a resposta TwiML
    twilio_response = MessagingResponse()
    
    # Processa a mensagem do usuário
    user_msg_raw = Body.strip() # Remove espaços extras no início/fim
    user_msg = user_msg_raw.lower() # Converte para minúsculas para comparação de comandos
    whatsapp_number = From.replace("whatsapp:", "") # Remove o prefixo "whatsapp:" do número

    try:
        # Lógica para o comando "INICIAR"
        if user_msg == "iniciar":
            # Verificar se o motorista já está cadastrado
            response_data, count = supabase.table('motoristas').select('whatsapp').eq('whatsapp', whatsapp_number).limit(1).execute()
            
            if response_data and response_data[1]: # Verifica se a lista de dados não está vazia
                twilio_response.message("Você já está cadastrado no Driverscash!")
            else:
                # Cadastra um novo motorista
                insert_data, count = supabase.table('motoristas').insert({"whatsapp": whatsapp_number, "plano": "essencial"}).execute()
                twilio_response.message(
                    "✅ Cadastro realizado! Use:\n"
                    "• \"GASTO 50.00 POSTO\" - Registrar despesas (use ponto para decimais)\n"
                    "• \"RELATORIO\" - Ver seus dados"
                )
        
        # Lógica para o comando "GASTO"
        elif user_msg.startswith("gasto "):
            parts = user_msg_raw.split(" ", 2) # Divide em no máximo 3 partes: 'gasto', 'valor', 'descrição'
            
            if len(parts) < 3: # Verifica se tem pelo menos valor e descrição
                twilio_response.message("❌ Formato incorreto para registrar gasto. Use: GASTO <VALOR> <DESCRICAO> (ex: GASTO 50.00 COMBUSTIVEL)")
            else:
                try:
                    # Tenta converter o valor para float, aceitando vírgula ou ponto como decimal
                    valor = float(parts[1].replace(",", "."))
                    descricao = parts[2].strip() # Pega a descrição e remove espaços extras
                    
                    if valor <= 0:
                        twilio_response.message("❌ O valor do gasto deve ser maior que zero.")
                    else:
                        # 1. VERIFICAR SE O MOTORISTA ESTÁ CADASTRADO E OBTER O ID
                        motorista_response, motorista_count = supabase.table('motoristas').select('id').eq('whatsapp', whatsapp_number).limit(1).execute()
                        
                        if motorista_response and motorista_response[1]:
                            motorista_id = motorista_response[1][0]['id'] # Pega o 'id' do motorista
                            
                            # 2. Insere a transação na tabela 'transacoes' (NÃO 'gastos') com o ID do usuário
                            insert_transacao_data, count_transacao = supabase.table('transacoes').insert({
                                "whatsapp": whatsapp_number,
                                "valor": valor,
                                "descricao": descricao,
                                "data": datetime.datetime.now().isoformat(), # Grava a data/hora atual
                                "id_do_usuario": motorista_id # CORRIGIDO: Agora usa 'id_do_usuario' (snake_case)
                            }).execute()
                            twilio_response.message(f"💰 Gasto de R${valor:.2f} para '{descricao}' registrado com sucesso!")
                        else:
                            twilio_response.message("❌ Você precisa se cadastrar primeiro para registrar gastos! Envie 'INICIAR'.")
                except ValueError:
                    twilio_response.message("❌ Valor inválido. Por favor, use um número. Ex: GASTO 50.50 ALMOCO")
                except Exception as e:
                    # Captura erros gerais durante o processo de gasto e imprime o traceback
                    print(f"Erro ao registrar gasto: {e}")
                    print(traceback.format_exc()) # Imprime o traceback completo para depuração
                    twilio_response.message("❌ Ocorreu um erro ao tentar registrar seu gasto. Por favor, tente novamente mais tarde.")

        # Lógica para o comando "RELATORIO"
        elif user_msg == "relatorio":
            # Busca as transacoes do motorista (NÃO 'gastos')
            response_data, count = supabase.table('transacoes').select('valor', 'descricao', 'data').eq('whatsapp', whatsapp_number).order('data', desc=True).execute()
            
            if response_data and response_data[1]:
                transacoes = response_data[1]
                total_transacoes = sum(t['valor'] for t in transacoes)
                
                relatorio_message = "📊 Seu relatório de transações:\n\n"
                for transacao in transacoes:
                    # Formata a data para melhor leitura
                    data_obj = datetime.datetime.fromisoformat(transacao['data'])
                    relatorio_message += f"• R${transacao['valor']:.2f} em {data_obj.strftime('%d/%m/%Y %H:%M')} ({transacao['descricao']})\n"
                
                relatorio_message += f"\nTotal: R${total_transacoes:.2f}"
                twilio_response.message(relatorio_message)
            else:
                twilio_response.message("Você ainda não possui transações registradas. Registre uma com: GASTO <VALOR> <DESCRICAO>")
        
        # Comando não reconhecido
        else:
            twilio_response.message(
                "⚠️ Comando inválido. Opções:\n"
                "• INICIAR - Começar cadastro\n"
                "• GASTO <VALOR> <DESCRICAO>\n"
                "• RELATORIO"
            )

    except Exception as e:
        # Loga o erro para depuração no Render
        print(f"Erro inesperado no webhook: {e}")
        print(traceback.format_exc()) # Imprime o traceback completo para depuração
        # Envia uma mensagem de erro genérica para o usuário
        twilio_response.message(f"❌ Ocorreu um erro interno no sistema. Por favor, tente novamente mais tarde.")

    # Retorna a resposta TwiML (XML) para o Twilio
    return Response(content=str(twilio_response), media_type="application/xml")

# Ponto de entrada para execução local (não usado no Render, mas útil para testes)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
