"""
Modern colorful logger for Quest Claimer
Thread-safe with beautiful formatting
"""
import threading
from datetime import datetime
from enum import Enum

class Colors:
    # Reset
    RESET = "\033[0m"
    
    # Regular colors
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    
    # Bright colors
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"
    
    # Background colors
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    
    # Styles
    BOLD = "\033[1m"
    DIM = "\033[2m"
    UNDERLINE = "\033[4m"


class LogLevel(Enum):
    INFO = "INFO"
    SUCCESS = "SUCCESS"
    WARNING = "WARN"
    ERROR = "ERROR"
    CAPTCHA = "CAPTCHA"
    CODE = "CODE"


_print_lock = threading.Lock()


def _get_timestamp():
    """Get current timestamp in HH:MM:SS format"""
    return datetime.now().strftime("%H:%M:%S")


def _get_level_style(level: LogLevel):
    """Get color and symbol for each log level"""
    styles = {
        LogLevel.INFO: (Colors.BRIGHT_CYAN, "●", Colors.CYAN),
        LogLevel.SUCCESS: (Colors.BRIGHT_GREEN, "✓", Colors.GREEN),
        LogLevel.WARNING: (Colors.BRIGHT_YELLOW, "⚠", Colors.YELLOW),
        LogLevel.ERROR: (Colors.BRIGHT_RED, "✗", Colors.RED),
        LogLevel.CAPTCHA: (Colors.BRIGHT_MAGENTA, "◐", Colors.MAGENTA),
        LogLevel.CODE: (Colors.BRIGHT_GREEN, "★", Colors.GREEN),
    }
    return styles.get(level, (Colors.WHITE, "•", Colors.WHITE))


def log(level: LogLevel, token: str, message: str, detail: str = None):
    color, symbol, msg_color = _get_level_style(level)
    timestamp = _get_timestamp()
    token_short = token[:26] if token else "--------"
    
    # Build the log line
    line_parts = [
        f"{Colors.DIM}{timestamp}{Colors.RESET}",
        f"{color}{Colors.BOLD}{symbol}{Colors.RESET}",
        f"{Colors.BRIGHT_BLACK}[{Colors.BRIGHT_WHITE}{token_short}{Colors.BRIGHT_BLACK}]{Colors.RESET}",
        f"{msg_color}{message}{Colors.RESET}",
    ]
    
    if detail:
        line_parts.append(f"{Colors.DIM}({detail}){Colors.RESET}")
    
    log_line = " ".join(line_parts)
    
    with _print_lock:
        print(log_line)


# Convenience functions
def info(token: str, message: str, detail: str = None):
    log(LogLevel.INFO, token, message, detail)

def ask(message: str, default: str = None) -> str:
    """
    Styled input prompt matching logger design
    Thread-safe
    """
    timestamp = _get_timestamp()

    prompt_parts = [
        f"{Colors.DIM}{timestamp}{Colors.RESET}",
        f"{Colors.BRIGHT_CYAN}{Colors.BOLD}?{Colors.RESET}",
        f"{Colors.BRIGHT_WHITE}{message}{Colors.RESET}",
    ]

    if default is not None:
        prompt_parts.append(
            f"{Colors.DIM}[default: {default}]{Colors.RESET}"
        )

    prompt = " ".join(prompt_parts) + " "

    with _print_lock:
        value = int(input(prompt).strip())

    if not value and default is not None:
        return default

    return value

def success(token: str, message: str, detail: str = None):
    log(LogLevel.SUCCESS, token, message, detail)


def warning(token: str, message: str, detail: str = None):
    log(LogLevel.WARNING, token, message, detail)


def error(token: str, message: str, detail: str = None):
    log(LogLevel.ERROR, token, message, detail)


def captcha(token: str, message: str, detail: str = None):
    log(LogLevel.CAPTCHA, token, message, detail)


def code(token: str, reward_code: str):
    """Special log for reward codes"""
    log(LogLevel.CODE, token, f"Got code: {Colors.BOLD}{Colors.BRIGHT_GREEN}{reward_code}{Colors.RESET}{Colors.GREEN}")


def print_banner():
    """Print a beautiful startup banner"""
    banner = f"""
{Colors.BRIGHT_CYAN}{Colors.BOLD}╔══════════════════════════════════════════════════════════╗
║                                                          ║
║   {Colors.BRIGHT_WHITE}🎮  VOID JOINER  🎮{Colors.BRIGHT_CYAN}                                    ║
║                                                          ║
╚══════════════════════════════════════════════════════════╝{Colors.RESET}
"""
    print(banner)


def print_config(tokens_count: int, proxies_count: int, threads: int):
    """Print configuration summary"""
    config = f"""
{Colors.BRIGHT_BLACK}┌──────────────────────────────────────┐{Colors.RESET}
{Colors.BRIGHT_BLACK}│{Colors.RESET}  {Colors.BRIGHT_CYAN}Tokens:{Colors.RESET}  {Colors.BRIGHT_WHITE}{tokens_count:>5}{Colors.RESET}                      {Colors.BRIGHT_BLACK}│{Colors.RESET}
{Colors.BRIGHT_BLACK}│{Colors.RESET}  {Colors.BRIGHT_CYAN}Proxies:{Colors.RESET} {Colors.BRIGHT_WHITE}{proxies_count:>5}{Colors.RESET}                      {Colors.BRIGHT_BLACK}│{Colors.RESET}
{Colors.BRIGHT_BLACK}│{Colors.RESET}  {Colors.BRIGHT_CYAN}Threads:{Colors.RESET} {Colors.BRIGHT_WHITE}{threads:>5}{Colors.RESET}                      {Colors.BRIGHT_BLACK}│{Colors.RESET}
{Colors.BRIGHT_BLACK}└──────────────────────────────────────┘{Colors.RESET}
"""
    print(config)


def print_separator():
    """Print a separator line"""
    print(f"{Colors.BRIGHT_BLACK}{'─' * 60}{Colors.RESET}")


def print_done(total: int, success_count: int, failed_count: int):
    """Print completion summary"""
    summary = f"""
{Colors.BRIGHT_BLACK}{'─' * 60}{Colors.RESET}
{Colors.BRIGHT_WHITE}{Colors.BOLD}  ✨ COMPLETED{Colors.RESET}
{Colors.BRIGHT_BLACK}{'─' * 60}{Colors.RESET}
  {Colors.BRIGHT_CYAN}Total:{Colors.RESET}   {Colors.BRIGHT_WHITE}{total}{Colors.RESET}
  {Colors.BRIGHT_GREEN}Success:{Colors.RESET} {Colors.BRIGHT_GREEN}{success_count}{Colors.RESET}
  {Colors.BRIGHT_RED}Failed:{Colors.RESET}  {Colors.BRIGHT_RED}{failed_count}{Colors.RESET}
{Colors.BRIGHT_BLACK}{'─' * 60}{Colors.RESET}
"""
    print(summary)
