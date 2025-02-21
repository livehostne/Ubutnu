import asyncio
import aiohttp
import json
from typing import Dict, List, Optional, Tuple
from pathlib import Path
import logging
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.prompt import Confirm
from rich.logging import RichHandler
from rich.theme import Theme
from datetime import datetime
import time
from urllib.parse import quote

console = Console(theme=Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "red",
    "success": "green"
}))

logging.basicConfig(
    level=logging.INFO,
    format="%(message)s",
    handlers=[RichHandler(console=console, rich_tracebacks=True)]
)
logger = logging.getLogger("rich")

def parse_upload_file(filepath: str) -> List[Dict]:
    """
    Processa o arquivo de upload e retorna uma lista de grupos.
    Cada grupo contém um nome de pasta e suas URLs.
    
    Formato esperado do arquivo:
    Nome: Nome da Pasta
    http://url1.com
    http://url2.com
    
    Nome: Outra Pasta
    http://url3.com
    http://url4.com
    """
    result = []
    current_folder = None
    current_urls = []
    
    try:
        with open(filepath, 'r', encoding='utf-8') as file:
            for line in file:
                line = line.strip()
                
                # Pula linhas vazias ou comentários
                if not line or line.startswith('_'):
                    if current_folder and current_urls:
                        result.append({
                            'folder_name': current_folder,
                            'urls': current_urls.copy()
                        })
                        current_folder = None
                        current_urls = []
                    continue
                
                # Processa nome da pasta
                if line.lower().startswith('nome:'):
                    if current_folder and current_urls:
                        result.append({
                            'folder_name': current_folder,
                            'urls': current_urls.copy()
                        })
                        current_urls = []
                    current_folder = line.split(':', 1)[1].strip()
                
                # Processa URLs
                elif line.startswith(('http://', 'https://')):
                    if current_folder:  # Só adiciona URLs se tiver uma pasta definida
                        current_urls.append(line)
        
        # Adiciona o último grupo se existir
        if current_folder and current_urls:
            result.append({
                'folder_name': current_folder,
                'urls': current_urls
            })
    
    except Exception as e:
        console.print(f"[red]Erro ao ler arquivo: {str(e)}")
        return []
    
    return result

class UploadManager:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://earnvidsapi.com/api"
        self.stats = {
            "total_uploads": 0,
            "successful_uploads": 0,
            "failed_uploads": 0,
            "total_folders": 0,
            "skipped_uploads": 0,
            "start_time": 0,
            "end_time": 0
        }
        self.MAX_UPLOADS = 480
        self.current_uploads = 0
        self.semaphore = asyncio.Semaphore(5)
        self.upload_results = []
        
    async def create_folder(self, session: aiohttp.ClientSession, folder_name: str, parent_id: int = 0) -> Optional[int]:
        retries = 3
        for attempt in range(retries):
            try:
                # Não codifica o nome da pasta, apenas caracteres especiais se necessário
                url = f"{self.base_url}/folder/create"
                params = {
                    "key": self.api_key,
                    "name": folder_name,  # Remove a codificação quote()
                    "parent_id": parent_id
                }
                
                logger.info(f"Criando pasta: '{folder_name}'")
                logger.info(f"URL da requisição: {url}")
                logger.info(f"Parâmetros: {params}")
                
                async with session.get(url, params=params) as response:
                    response_text = await response.text()
                    logger.info(f"Resposta da API (criar pasta): {response_text}")
                    
                    if response.status == 503:
                        logger.warning(f"Servidor indisponível (tentativa {attempt + 1}/{retries})")
                        if attempt < retries - 1:
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        logger.error(f"Servidor indisponível após {retries} tentativas para '{folder_name}'")
                        return None
                    
                    try:
                        data = json.loads(response_text)
                        if "result" in data and "fld_id" in data["result"]:
                            folder_id = data["result"]["fld_id"]
                            self.stats["total_folders"] += 1
                            logger.info(f"Pasta criada com sucesso: {folder_name} (ID: {folder_id})")
                            return folder_id
                    except json.JSONDecodeError as e:
                        logger.error(f"Erro ao decodificar resposta JSON: {e}")
                        logger.error(f"Resposta raw: {response_text}")
                        continue
                    
                    logger.error(f"Erro ao criar pasta. Resposta: {data}")
                    return None
                    
            except Exception as e:
                logger.error(f"Exceção ao criar pasta '{folder_name}': {str(e)}")
                if attempt < retries - 1:
                    continue
                return None
        return None

    async def upload_file(self, session: aiohttp.ClientSession, file_url: str, folder_id: int) -> Tuple[bool, str]:
        if self.current_uploads >= self.MAX_UPLOADS:
            logger.warning(f"Limite de uploads atingido ({self.MAX_UPLOADS})")
            self.stats["skipped_uploads"] += 1
            return False, "Limite de uploads atingido"

        async with self.semaphore:
            retries = 3
            for attempt in range(retries):
                try:
                    logger.info(f"Iniciando upload: {file_url}")
                    
                    # Verifica se arquivo existe
                    if not await self.verify_upload(session, file_url):
                        logger.error(f"Arquivo não encontrado: {file_url}")
                        return False, "Arquivo não encontrado"

                    url = f"{self.base_url}/upload/url"
                    params = {
                        "key": self.api_key,
                        "url": file_url,  # Remove a codificação quote()
                        "fld_id": folder_id
                    }
                    
                    logger.info(f"URL da requisição: {url}")
                    logger.info(f"Parâmetros: {params}")
                    
                    async with session.get(url, params=params) as response:
                        response_text = await response.text()
                        logger.info(f"Resposta da API (upload): {response_text}")
                        
                        if response.status == 503:
                            logger.warning(f"Servidor indisponível (tentativa {attempt + 1}/{retries})")
                            if attempt < retries - 1:
                                await asyncio.sleep(2 * (attempt + 1))
                                continue
                            return False, "Servidor temporariamente indisponível"
                        
                        try:
                            data = json.loads(response_text)
                            if "result" in data:
                                self.current_uploads += 1
                                self.stats["successful_uploads"] += 1
                                logger.info(f"Upload realizado com sucesso: {file_url}")
                                return True, "Upload realizado com sucesso"
                            else:
                                logger.error(f"Erro na resposta da API: {data}")
                                return False, f"Erro: {data.get('message', 'Desconhecido')}"
                        except json.JSONDecodeError as e:
                            logger.error(f"Erro ao decodificar resposta JSON: {e}")
                            logger.error(f"Resposta raw: {response_text}")
                            if attempt < retries - 1:
                                continue
                            return False, "Resposta inválida do servidor"
                        
                except Exception as e:
                    logger.error(f"Exceção durante upload: {str(e)}")
                    if attempt < retries - 1:
                        continue
                    return False, f"Erro: {str(e)}"
            
            self.stats["failed_uploads"] += 1
            return False, "Todas as tentativas falharam"

async def process_group(manager: UploadManager, session: aiohttp.ClientSession, group: Dict, progress) -> None:
    folder_name = group['folder_name']
    urls = group['urls']
    
    folder_id = await manager.create_folder(session, folder_name)
    if not folder_id:
        return

    upload_tasks = []
    for url in urls:
        if manager.current_uploads >= manager.MAX_UPLOADS:
            console.print(f"[yellow]⚠️ Limite de {manager.MAX_UPLOADS} uploads atingido. Restante será ignorado.")
            break
            
        task = asyncio.create_task(manager.upload_file(session, url, folder_id))
        upload_tasks.append((url, task))
        
    for url, task in upload_tasks:
        success, message = await task
        status = "[green]✓" if success else "[red]✗"
        console.print(f"{status} {url}: {message}")
        progress.update(progress.task_ids[0], advance=1)
        
        manager.upload_results.append({
            "url": url,
            "success": success,
            "message": message,
            "folder": folder_name
        })

def save_results(results: List[Dict], filename: str = "upload_results.json"):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)

def show_summary(stats: Dict):
    duration = stats["end_time"] - stats["start_time"]
    uploads_per_second = stats["successful_uploads"] / duration if duration > 0 else 0
    
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Estatísticas", style="dim")
    table.add_column("Valor", justify="right")
    
    table.add_row("Total de Pastas", str(stats["total_folders"]))
    table.add_row("Uploads com Sucesso", f"[green]{stats['successful_uploads']}")
    table.add_row("Uploads com Falha", f"[red]{stats['failed_uploads']}")
    table.add_row("Uploads Ignorados", f"[yellow]{stats['skipped_uploads']}")
    table.add_row("Tempo Total", f"{duration:.2f}s")
    table.add_row("Velocidade", f"{uploads_per_second:.2f} uploads/s")
    
    console.print(Panel(table, title="📊 Resumo", border_style="cyan"))

async def main():
    API_KEY = "37873r42y3ohehkaijlx7"
    UPLOAD_FILE = "upload_list.txt"
    
    uploader = UploadManager(API_KEY)
    uploader.show_welcome()
    
    if not uploader.verify_api_key():
        return
    
    if not Path(UPLOAD_FILE).exists():
        console.print(f"[bold red]❌ Arquivo {UPLOAD_FILE} não encontrado!")
        return
    
    upload_groups = parse_upload_file(UPLOAD_FILE)
    if not upload_groups:
        console.print("[bold red]❌ Nenhum grupo de upload encontrado!")
        return
    
    total_files = sum(len(group['urls']) for group in upload_groups)
    console.print(f"[cyan]Arquivos encontrados: {total_files}")
    console.print(f"[yellow]Nota: Máximo de {uploader.MAX_UPLOADS} uploads serão processados")
    
    if not Confirm.ask("\n💫 Iniciar uploads?"):
        return
    
    timeout = aiohttp.ClientTimeout(total=None, connect=60, sock_connect=60, sock_read=60)
    connector = aiohttp.TCPConnector(limit=None, ttl_dns_cache=300)
    
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console
        ) as progress:
            total_progress = progress.add_task("[cyan]Progresso", total=min(total_files, uploader.MAX_UPLOADS))
            
            uploader.stats["start_time"] = time.time()
            
            tasks = []
            for group in upload_groups:
                task = asyncio.create_task(process_group(uploader, session, group, progress))
                tasks.append(task)
            
            await asyncio.gather(*tasks)
            
            uploader.stats["end_time"] = time.time()
    
    show_summary(uploader.stats)
    
    # Salva os resultados em um arquivo JSON
    save_results(uploader.upload_results)
    console.print("\n[green]✓ Resultados salvos em 'upload_results.json'")
    
    console.print("\n[bold green]✨ Upload Manager finalizado com sucesso! ✨")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️ Interrompido pelo usuário")
    except Exception as e:
        console.print(f"\n[red]❌ Erro: {str(e)}")
    finally:
        console.print("\n[dim]Pressione Enter para sair...")
        input()
