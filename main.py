from fastapi import FastAPI, Request, Response
from supabase import create_client
import os
from dotenv import load_dotenv

# Configurações iniciais
load_dotenv()

app = FastAPI()

# Conexão com Supabase (versão compatível)
supabase = create_client(
    supabase_url=os.getenv("SUPABASE_URL"),
    supabase_key=os.getenv("SUPABASE_KEY")
)

@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    try:
        # Recebe dados do WhatsApp
        form_data = await request.form()
        user_msg = form_data.get('Body', '').lower()
        user_number = form_data.get('From', '')
        
        # Comandos disponíveis
        if user_msg.startswith("iniciar"):
            # Cadastra novo motorista
            supabase.table("motoristas").insert({
                "whatsapp": user_number,
                "plano": "essencial"
            }).execute()
            
            return Response("""
            ✅ Cadastro realizado! Use:
            • "GASTO 50 POSTO" - Registrar despesas
            • "RELATORIO" - Ver seus dados
            """)

        elif user_msg.startswith("gasto"):
            # Registra gastos (ex: "gasto 50 combustivel")
            parts = user_msg.split()
            if len(parts) >= 2:
                supabase.table("gastos").insert({
                    "whatsapp": user_number,
                    "valor": float(parts[1]),
                    "descricao": " ".join(parts[2:]) if len(parts) > 2 else "Não especificado"
                }).execute()
                return Response(f"✅ Gastos: R${parts[1]} - {' '.join(parts[2:])}")
            else:
                return Response("⚠️ Formato incorreto. Use: GASTO [valor] [descrição]")

        else:
            return Response("""
            ⚠️ Comando inválido. Opções:
            • INICIAR - Começar cadastro
            • GASTO [valor] [motivo]
            """)

    except Exception as e:
        return Response(f"❌ Erro: {str(e)}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)