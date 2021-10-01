#!/usr/bin/env python3

import time, sys, os, uuid, tempfile, re, subprocess, json, logging, datetime, multiprocessing, argparse, shutil
import requests
import yfinance as yf
import mplfinance as mpf
import redis

import locale
locale.setlocale(locale.LC_ALL, "en_US.utf8")

class tickergram:
    def __init__(self, tg_token, redis_host, redis_port, redis_db, password=""):
        # Configuration
        self.BOT_PASSWORD = password
        self.BOT_ENABLED_PASS = True if password else False
        self.REDIS_HOST = redis_host
        self.REDIS_PORT = redis_port
        self.REDIS_DB = redis_db
        self.TG_API="https://api.telegram.org/bot" + tg_token
        self.MAX_CHART_TIME = datetime.timedelta(days=3*365) # 3 years
        # Configure logging
        self.logger = logging.getLogger("tickergram_log")
        self.logger.setLevel(logging.DEBUG)
        logger_fh = logging.FileHandler("tickergram.log")
        logger_fh.setLevel(logging.DEBUG)
        logger_ch = logging.StreamHandler()
        logger_ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(asctime)s %(message)s")
        logger_fh.setFormatter(formatter)
        logger_ch.setFormatter(formatter)
        self.logger.addHandler(logger_fh)
        self.logger.addHandler(logger_ch)
        # Anti flood protection
        self.antiflood_cache = {}
        self.ANTI_FLOOD_SECS = 1

    def tg_getme(self):
        r = requests.get(self.TG_API+"/getMe")
        d = r.json()
        if not d["ok"]:
            return False
        return d

    def tg_send_msg(self, text, chat):
        d = {"chat_id": chat, "text": text, "parse_mode": "MarkdownV2"}
        r = requests.get(self.TG_API+"/sendMessage", params=d)
        d = r.json()
        if not d["ok"]:
            return False
        return d

    def tg_send_msg_post(self, text, chat):
        d = {"chat_id": chat, "text": text, "parse_mode": "MarkdownV2"}
        r = requests.post(self.TG_API+"/sendMessage", params=d)
        d = r.json()
        if not d["ok"]:
            return False
        return d

    def tg_chat_exists(self, chat_id):
        d = {"chat_id": chat_id}
        r = requests.get(self.TG_API+"/getChat", params=d)
        d = r.json()
        if d["ok"]:
            return True
        elif not d["ok"] and d["error_code"] == 400:
            return False
        else:
            raise RuntimeError("tg_chat_exists not ok")
        return d

    def tg_delete_msg(self, tg_message):
        d = {"chat_id": tg_message["chat"]["id"], "message_id": tg_message["message_id"]}
        r = requests.get(self.TG_API+"/deleteMessage", params=d)
        d = r.json()
        if not d["ok"]:
            raise RuntimeError("tg_delete_msg not ok")
        return d

    def tg_send_pic(self, f, chat):
        d = {"chat_id": chat}
        r = requests.post(self.TG_API+"/sendPhoto", data=d, files={"photo": open(f, "rb")})
        d = r.json()
        if not d["ok"]:
            raise RuntimeError("tg_send_pic not ok")
        return d

    def tg_get_messages(self, offset=0, limit=1):
        d = {"timeout": 300, "allowed_updates": ["message"], "limit": limit}
        if offset:
            d["offset"] = offset
        r = requests.get(self.TG_API+"/getUpdates", params=d)
        d = r.json()
        if not d["ok"]:
            raise RuntimeError("tg_get_messages not ok")
        return d

    def redis_get_db(self):
        return redis.Redis(host=self.REDIS_HOST,
                port=self.REDIS_PORT, db=self.REDIS_DB)

    def redis_ping(self):
        return self.redis_get_db().ping()

    def redis_add_chat_auth(self, chat_id):
        r = self.redis_get_db()
        r.sadd("auth_chats", chat_id)

    def redis_check_chat_auth(self, chat_id):
        return self.redis_get_db().sismember("auth_chats", chat_id)

    def redis_user_watch_info_exists(self, chat_id):
        r = self.redis_get_db()
        return r.exists("wl_{}_info".format(chat_id))

    def redis_user_watch_info_save(self, chat_id, info):
        r = self.redis_get_db()
        r.set("wl_{}_info".format(chat_id), json.dumps(info))

    def redis_add_user_watch(self, ticker, chat_id):
        r = self.redis_get_db()
        r.sadd("wl_{}".format(chat_id), ticker)

    def redis_del_user_watch(self, ticker, chat_id):
        r = self.redis_get_db()
        r.srem("wl_{}".format(chat_id), ticker)

    def redis_list_user_watch(self, chat_id):
        r = self.redis_get_db()
        return sorted(r.smembers("wl_{}".format(chat_id)))

    def redis_watch_toggle(self, chat_id):
        r = self.redis_get_db()
        if r.sismember("wl_enabled", chat_id):
            r.srem("wl_enabled", chat_id)
            return False
        else:
            r.sadd("wl_enabled", chat_id)
            return True

    def redis_watch_disable(self, chat_id):
        r = self.redis_get_db()
        r.srem("wl_enabled", chat_id)

    def redis_list_enabled_watchlists(self):
        r = self.redis_get_db()
        return r.smembers("wl_enabled")

    def redis_get_feargreed_cache(self):
        return self.redis_get_db().get("feargreed_cache")

    def redis_set_feargreed_cache(self, img_data):
        r = self.redis_get_db()
        r.setex("feargreed_cache", 10800, img_data) # 3 hour exp

    def redis_get_quote_cache(self, ticker):
        d = self.redis_get_db().get("quote_"+ticker)
        return json.loads(d) if d else None

    def redis_set_quote_cache(self, ticker, ticker_data):
        r = self.redis_get_db()
        r.setex("quote_"+ticker, 300, json.dumps(ticker_data)) # 5 min exp

    def test_tg_or_die(self):
        self.logger.info("Checking Telegram API token ...")
        if not self.tg_getme():
            self.logger.error("Telegram API token is invalid, exiting ...")
            sys.exit(1)
        self.logger.info("Telegram API token is valid")

    def test_redis_or_die(self):
        self.logger.info("Testing Redis connectivity ...")
        if not self.redis_ping():
            self.logger.error("Unable to connect to Redis, exiting ...")
            sys.exit(1)
        self.logger.info("Redis connection is ok")

    def write_pidfile(self):
        pidf = os.path.join(tempfile.gettempdir(), "tickergram.pid")
        pid = os.getpid()
        with open(pidf, "w") as f:
            f.write(str(pid))
        return pid

    def ticker_add_emoji(self, ticker):
        emoji_dict = {"SPY": u"\U0001F1FA\U0001F1F8", "QQQ": u"\U0001F4BB", "MCHI": u"\U0001F1E8\U0001F1F3",
                "FEZ": u"\U0001F1EA\U0001F1FA", "BTC-USD": u"\U000020BF ", "GC=F": u"\U0001F947",
                "VNQ": u"\U0001F3E0", "^TNX": u"\U0001F4B5"}
        return emoji_dict.get(ticker, "") + ticker

    def ticker_chg_emoji_color(self, sign):
        return u"\U0001F7E2" if sign == "+" else u"\U0001F534"

    def text_quote_long(self, t, short_name, price, price_prevclose, price_change, ftweek_high, ftweek_high_chg, ftweek_low, ftweek_low_chg, volume,
            volume_avg, pe, pe_forward, div_yield):
        price_chg_sign = "+" if price >= price_prevclose else "-"
        ftweek_high_chg_sign = "+" if price >= ftweek_high else "-"
        ftweek_low_chg_sign = "+" if price >= ftweek_low else "-"
        if price_change > 1:
            price_change_emoji = u"\U0001F4C9" if price_chg_sign == "-" else u"\U0001F680"
        else:
            price_change_emoji = ""
        ftweek_high_chg_emoji = ""+(u"\U00002757"*int(ftweek_high_chg/10))
        text_msg = "```\n"
        text_msg += "{}\n".format(short_name)
        text_msg += "{}{} {:.2f} ({}{:.2f}%{})\n".format(self.ticker_chg_emoji_color(price_chg_sign), t, price, price_chg_sign, price_change, price_change_emoji)
        text_msg += "-"*len(t) + "\n"
        text_msg += "52w high {:.2f} ({}{:.2f}%{})\n".format(ftweek_high, ftweek_high_chg_sign, ftweek_high_chg, ftweek_high_chg_emoji)
        text_msg += "52w low {:.2f} ({}{:.2f}%)\n".format(ftweek_low, ftweek_low_chg_sign, ftweek_low_chg)
        text_msg += "volume {}\n".format(volume)
        text_msg += "volume average {}\n".format(volume_avg)
        text_msg += "PE ratio {}\n".format(pe)
        text_msg += "PE ratio forward {}\n".format(pe_forward)
        text_msg += "Dividend yield {}\n".format(div_yield)
        text_msg += "\n```"
        return text_msg

    def text_quote_short(self, t, price, price_prevclose, price_change, ftweek_high, ftweek_high_chg):
        price_change_sign = "+" if price >= price_prevclose else "-"
        if price_change > 1:
            price_change_emoji = u"\U0001F4C9" if price_change_sign == "-" else u"\U0001F680"
        else:
            price_change_emoji = ""
        ftweek_high_chg_sign = "+" if price >= ftweek_high else "-"
        ftweek_high_chg_emoji = ""+(u"\U00002757"*int(ftweek_high_chg/10))
        text_msg = "{}{} {:.2f} ({}{:.2f}%{} 52w high chg {}{:.2f}%{})\n".format(self.ticker_chg_emoji_color(price_change_sign), self.ticker_add_emoji(t),
                price, price_change_sign, price_change, price_change_emoji, ftweek_high_chg_sign, ftweek_high_chg, ftweek_high_chg_emoji)
        return text_msg

    def generic_get_quote(self, ticker):
        # Easily replace the quote provider here, using the same standard
        # output format used in yf_get_quote
        return self.yf_get_quote(ticker)

    def yf_get_quote(self, ticker):
        # Get ticker cache before querying YF
        quote_cache = self.redis_get_quote_cache(ticker)
        if quote_cache:
            return quote_cache
        ret_data = {}
        try:
            ty = yf.Ticker(ticker)
            ty_info = ty.info
        except:
            return None
        if "shortName" not in ty_info.keys() or not ty_info.get("regularMarketPrice"):
            return None
        ret_data["company_name"] = ty_info["shortName"]
        ret_data["latest_price"] = round(ty_info["regularMarketPrice"], 2)
        ret_data["previous_close"] = round(ty_info["previousClose"], 2)
        ret_data["52w_high"] = round(ty_info["fiftyTwoWeekHigh"], 2)
        ret_data["52w_low"] = round(ty_info["fiftyTwoWeekLow"], 2)
        ret_data["market_volume"] = ty_info["regularMarketVolume"]
        ret_data["market_volume"] = f'{ret_data["market_volume"]:n}'
        ret_data["market_volume_avg"] = ty_info["averageVolume"]
        ret_data["market_volume_avg"] = f'{ret_data["market_volume_avg"]:n}'
        pe = ty_info.get("trailingPE", None)
        pe = round(pe, 2) if pe else "N/A"
        ret_data["pe_trailing"] = pe
        pe_forward = ty_info.get("forwardPE", None)
        pe_forward = round(pe_forward, 2) if pe_forward else "N/A"
        ret_data["pe_forward"] = pe_forward
        div_yield = ty_info.get("dividendYield", None)
        div_yield = "{}%".format(round(div_yield*100, 2)) if div_yield else "N/A"
        ret_data["div_yield"] = div_yield
        self.redis_set_quote_cache(ticker, ret_data)
        return ret_data

    def yf_get_stock_chart(self, ticker, time_range="1Y"):
        time_range = time_range.replace("M", "MO")
        output_file = "{}.png".format(str(uuid.uuid4()))
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period=time_range)
            mpf.plot(hist, type="candle", volume=True, style="mike", datetime_format='%b %Y',
                    figratio=(20,10), tight_layout=True,
                    title="\n{} {}".format(ticker, time_range),
                    savefig=dict(fname=output_file, dpi=95))
        except:
            pass
        return output_file

    def cnn_get_fear_greed(self):
        output_file = "{}.png".format(str(uuid.uuid4()))
        cache_pic = self.redis_get_feargreed_cache()
        if cache_pic:
            with open(output_file, "wb") as f:
                f.write(cache_pic)
        else:
            self.ff_screenshot("https://money.cnn.com/data/fear-and-greed/", "660,470", output_file)
            if os.path.exists(output_file):
                with open(output_file, "rb") as f:
                    self.redis_set_feargreed_cache(f.read())
        return output_file

    def ff_screenshot(self, url, ws, output):
        profile = str(uuid.uuid4())
        profile_path = os.path.join(tempfile.gettempdir(), profile)
        os.mkdir(profile_path)
        try:
            subprocess.run(["firefox", "--headless", "--kiosk", "--profile", profile_path,
                "--window-size={}".format(ws), "--screenshot", output, url], timeout=300)
            shutil.rmtree(profile_path)
        except:
            pass

    def get_change(self, current, previous):
        if current == previous:
            return 0
        try:
            return round((abs(current - previous) / previous) * 100.0, 2)
        except ZeroDivisionError:
            return float("inf")

    def valid_ticker(self, ticker):
        return True if len(ticker) <= 8 and re.fullmatch(r"^[A-Za-z0-9\.\^\-]{1,8}$", ticker) else False

    def bot_watchlist_notify(self, chat_id=None):
        if chat_id:
            # Craft a fake watchlist list for this chat
            watchlists = [str(chat_id)]
        else:
            # This function was called from notify_watchers()
            self.test_tg_or_die()
            self.test_redis_or_die()
            watchlists = self.redis_list_enabled_watchlists()
        for chat_id in watchlists:
            chat_id = chat_id.decode() if type(chat_id) is not str else chat_id
            if not self.tg_chat_exists(int(chat_id)):
                # Chat doesn't exist anymore, disable automatic notifications for this watchlist
                self.redis_watch_disable(chat_id)
                continue
            wl_tickers = self.redis_list_user_watch(chat_id)
            if not wl_tickers:
                continue
            text_msg = "```\n"
            for t in wl_tickers:
                t = t.decode()
                ticker_info = self.generic_get_quote(t)
                if not ticker_info:
                    continue
                price = ticker_info["latest_price"]
                price_prevclose = ticker_info["previous_close"]
                ftweek_high = ticker_info["52w_high"]
                # Get price changes
                price_change = self.get_change(price, price_prevclose)
                ftweek_high_chg = self.get_change(price, ftweek_high)
                # Compose message text
                text_msg += self.text_quote_short(t, price, price_prevclose, price_change, ftweek_high, ftweek_high_chg)
            text_msg += "```"
            if not self.tg_send_msg_post(text_msg, chat_id):
                # Error delivering message, disable automatic notifications for this watchlist
                self.redis_watch_disable(chat_id)

    def bot_auth_chat(self, chat):
        return self.redis_check_chat_auth(chat["id"])

    def bot_antiflood_check(self, msg_from, msg_date):
        for u in list(self.antiflood_cache.keys()):
            if self.antiflood_cache[u] + self.ANTI_FLOOD_SECS < msg_date:
                del self.antiflood_cache[u]
        hit_antiflood = msg_from["id"] in self.antiflood_cache.keys()
        self.antiflood_cache[msg_from["id"]] = msg_date
        return hit_antiflood

    def bot_cmd_help(self, chat, text, msg_from):
        text_msg = "/help show this help message\n"
        if self.BOT_ENABLED_PASS:
            text_msg += "/auth *\<password\>* authorize chat to use this bot if password is correct\n"
        text_msg += "/quote *\<symbol\>* get quote\n"
        text_msg += "/chart *\<symbol\> \[1y,6m,5d\]* get price and volume chart\n"
        text_msg += "/watch *list\|add\|del* *\[symbol\]* list, add or remove symbol from your watchlist\n"
        text_msg += "/watchlist get an overview of your watchlist\n"
        text_msg += "/watchlistnotify toggle the automatic watchlist notifications on and off\n"
        text_msg += "/overview get an overview of global ETFs\n"
        text_msg += "/feargreed get picture of CNN's Fear & Greed Index\n\n"
        text_msg += u"_Powered by [Tickergram](https://github.com/a0rtega/tickergram-bot)_"
        self.tg_send_msg_post(text_msg, chat["id"])

    def bot_cmd_auth(self, chat, text, msg_from):
        auth_pwd = text.replace("/auth ", "")
        if auth_pwd == self.BOT_PASSWORD:
            self.redis_add_chat_auth(chat["id"])
            text_msg = "```\nChat access granted, welcome {}\n```".format(msg_from["first_name"])
        else:
            text_msg = "```\nInvalid password\n```"
        self.tg_send_msg_post(text_msg, chat["id"])

    def bot_cmd_quote(self, chat, text, msg_from):
        ticker = text.replace("/quote ", "").upper()
        if self.valid_ticker(ticker):
            proc_msg = self.tg_send_msg_post("```\nProcessing, please wait ...\n```", chat["id"])["result"]
            ticker_info = self.generic_get_quote(ticker)
            if ticker_info:
                short_name = ticker_info["company_name"]
                price = ticker_info["latest_price"]
                price_prevclose = ticker_info["previous_close"]
                ftweek_high = ticker_info["52w_high"]
                ftweek_low = ticker_info["52w_low"]
                volume = ticker_info["market_volume"]
                volume_avg = ticker_info["market_volume_avg"]
                pe = ticker_info["pe_trailing"]
                pe_forward = ticker_info["pe_forward"]
                div_yield = ticker_info["div_yield"]
                # Get price changes
                price_change = self.get_change(price, price_prevclose)
                ftweek_high_chg = self.get_change(price, ftweek_high)
                ftweek_low_chg = self.get_change(price, ftweek_low)
                # Compose message text
                text_msg = self.text_quote_long(ticker, short_name, price, price_prevclose, price_change, ftweek_high, ftweek_high_chg, ftweek_low, ftweek_low_chg, volume,
                        volume_avg, pe, pe_forward, div_yield)
            else:
                text_msg = "```\nError getting ticker info\n```"
            self.tg_delete_msg(proc_msg)
        else:
            text_msg = "```\nInvalid ticker\n```"
        self.tg_send_msg_post(text_msg, chat["id"])

    def bot_cmd_watch(self, chat, text, msg_from):
        cmd = text.replace("/watch ", "").split(" ")
        if cmd[0] == "list":
            watchlist = self.redis_list_user_watch(chat["id"])
            watchlist = ", ".join([w.decode() for w in watchlist]) if watchlist else "empty"
            text_msg = "```\nYour watchlist is {}\n```".format(watchlist)
        elif cmd[0] == "add" and len(cmd) == 2:
            ticker = cmd[1].upper()
            if len(self.redis_list_user_watch(chat["id"])) <= 50:
                if self.valid_ticker(ticker):
                    proc_msg = self.tg_send_msg_post("```\nProcessing, please wait ...\n```", chat["id"])["result"]
                    ticker_info = self.generic_get_quote(ticker)
                    if ticker_info:
                        if not self.redis_user_watch_info_exists(chat["id"]):
                            self.redis_user_watch_info_save(chat["id"], {"chat":chat, "msg_from":msg_from})
                        self.redis_add_user_watch(ticker, chat["id"])
                        text_msg = "```\n{} added to your watchlist\n```".format(ticker)
                    else:
                        text_msg = "```\nError getting ticker info\n```"
                    self.tg_delete_msg(proc_msg)
                else:
                    text_msg = "```\nInvalid ticker\n```"
            else:
                text_msg = "```\nWatchlist maximum limit hit\n```"
        elif cmd[0] == "del" and len(cmd) == 2:
            ticker = cmd[1].upper()
            if self.valid_ticker(ticker):
                self.redis_del_user_watch(ticker, chat["id"])
                text_msg = "```\n{} removed from your watchlist\n```".format(ticker)
            else:
                text_msg = "```\nInvalid ticker\n```"
        else:
            text_msg = "```\nInvalid watch command\n```"
        self.tg_send_msg_post(text_msg, chat["id"])

    def bot_cmd_watchlist(self, chat, text, msg_from):
        proc_msg = self.tg_send_msg_post("```\nProcessing, please wait ...\n```", chat["id"])["result"]
        if not self.redis_list_user_watch(chat["id"]):
            text_msg = "```\nYour watchlist is empty\n```"
            self.tg_send_msg_post(text_msg, chat["id"])
        else:
            self.bot_watchlist_notify(chat["id"])
        self.tg_delete_msg(proc_msg)

    def bot_cmd_watchlistnotify(self, chat, text, msg_from):
        status = self.redis_watch_toggle(chat["id"])
        status = "enabled" if status else "disabled"
        text_msg = "```\nWatchlist notifications are now {}\n```".format(status)
        self.tg_send_msg_post(text_msg, chat["id"])

    def bot_cmd_chart(self, chat, text, msg_from):
        request = text.replace("/chart ", "").split(" ")
        ticker = request[0].upper()
        if len(request) > 1:
            time_range = request[1].upper()
        else:
            time_range = "1Y"

        if not self.valid_ticker(ticker):
            text_msg = "```\nInvalid ticker\n```"
            self.tg_send_msg_post(text_msg, chat["id"])
            return
        if not re.fullmatch(r"^\d{1,3}(Y|M|D)$", time_range):
            text_msg = "```\nInvalid time range\n```"
            self.tg_send_msg_post(text_msg, chat["id"])
            return

        time_range_int = int(time_range[:-1])
        if time_range.endswith("Y"):
            chart_td = datetime.timedelta(days=time_range_int*365)
        elif time_range.endswith("M"):
            chart_td = datetime.timedelta(days=time_range_int*30)
        else:
            chart_td = datetime.timedelta(days=time_range_int)
        if (self.MAX_CHART_TIME - chart_td) < datetime.timedelta(0):
            text_msg = "```\nChart time range exceeds the limit\n```"
            self.tg_send_msg_post(text_msg, chat["id"])
            return

        proc_msg = self.tg_send_msg_post("```\nProcessing, please wait ...\n```", chat["id"])["result"]
        output_pic = self.yf_get_stock_chart(ticker, time_range)
        if os.path.exists(output_pic):
            self.tg_send_pic(output_pic, chat["id"])
            os.remove(output_pic)
        else:
            text_msg = "```\nError\n```"
            self.tg_send_msg_post(text_msg, chat["id"])
        self.tg_delete_msg(proc_msg)

    def bot_cmd_overview(self, chat, text, msg_from):
        global_tickers = ["#Stocks ETFs", "SPY", "QQQ", "FEZ", "MCHI", "VNQ", "#10Y Bonds", "^TNX", "#Gold", "GC=F", "#Crypto", "BTC-USD"]
        proc_msg = self.tg_send_msg_post("```\nProcessing, please wait ...\n```", chat["id"])["result"]
        try:
            text_msg = "```\n"
            for t in global_tickers:
                if t.startswith("#"): # Parse sections
                    if len(t) > 1:
                        text_msg += "----- {}\n".format(t[1:])
                    else:
                        text_msg += "-----\n"
                    continue
                ticker_info = self.generic_get_quote(t)
                price = ticker_info["latest_price"]
                price_prevclose = ticker_info["previous_close"]
                ftweek_high = ticker_info["52w_high"]
                # Get price changes
                price_change = self.get_change(price, price_prevclose)
                ftweek_high_chg = self.get_change(price, ftweek_high)
                # Compose message text
                text_msg += self.text_quote_short(t, price, price_prevclose, price_change, ftweek_high, ftweek_high_chg)
            text_msg += "```"
        except Exception as e:
            self.logger.error(str(e))
            text_msg = "```\nError\n```"
        self.tg_send_msg_post(text_msg, chat["id"])
        self.tg_delete_msg(proc_msg)

    def bot_cmd_feargreed(self, chat, text, msg_from):
        proc_msg = self.tg_send_msg_post("```\nProcessing, please wait ...\n```", chat["id"])["result"]
        output_pic = self.cnn_get_fear_greed()
        if os.path.exists(output_pic):
            self.tg_send_pic(output_pic, chat["id"])
            os.remove(output_pic)
        else:
            text_msg = "```\nError\n```"
            self.tg_send_msg_post(text_msg, chat["id"])
        self.tg_delete_msg(proc_msg)

    def bot_cmd_handler(self, fnc, chat, text, msg_from):
        p = multiprocessing.Process(target=fnc, args=(chat, text, msg_from),
                daemon=True)
        p.start()

    def bot_loop(self):
        self.test_tg_or_die()
        self.test_redis_or_die()
        self.logger.info("Bot is running with pid {}".format(self.write_pidfile()))
        last_update_id = 0
        while True:
            try:
                msgs = self.tg_get_messages(offset=last_update_id)
            except:
                self.logger.error("Unable to query Telegram Bot API")
                time.sleep(30)
                continue
            for m in msgs["result"]:
                try:
                    update_id = m["update_id"]
                    chat = m["message"]["chat"]
                    text = m["message"]["text"]
                    msg_from = m["message"]["from"]
                    msg_date = m["message"]["date"]
                except Exception as e:
                    self.logger.error("Error parsing update: {}".format(m))
                    last_update_id = update_id + 1
                    continue
                self.logger.debug("{} {} {}".format(msg_from, chat, text))
                hit_antiflood = self.bot_antiflood_check(msg_from, msg_date)
                if hit_antiflood:
                    self.logger.warning("User hit antiflood protection")
                    # Increase update id
                    last_update_id = update_id + 1
                    continue
                # Check chat authorization if enabled
                chat_auth = True
                if self.BOT_ENABLED_PASS:
                    chat_auth = self.bot_auth_chat(chat)
                    if not chat_auth:
                        self.logger.warning("Message from unauthorized chat: {} {}".format(msg_from, text))
                # Handle commands
                if text in ("/help", "/start"):
                    self.bot_cmd_help(chat, text, msg_from)
                elif self.BOT_ENABLED_PASS and text.startswith("/auth "):
                    self.bot_cmd_auth(chat, text, msg_from)
                else: # Authorized-only commands
                    if not chat_auth and text.split(" ")[0] in ("/quote", "/chart",
                            "/watch", "/watchlist", "/watchlistnotify",
                            "/overview", "/feargreed"):
                        text_msg = "```\nUnauthorized\n```"
                        self.tg_send_msg_post(text_msg, chat["id"])
                    elif chat_auth and text.startswith("/quote "):
                        self.bot_cmd_handler(self.bot_cmd_quote, chat, text, msg_from)
                    elif chat_auth and text.startswith("/chart "):
                        self.bot_cmd_handler(self.bot_cmd_chart, chat, text, msg_from)
                    elif chat_auth and text.startswith("/watch "):
                        self.bot_cmd_handler(self.bot_cmd_watch, chat, text, msg_from)
                    elif chat_auth and text == "/watchlist":
                        self.bot_cmd_handler(self.bot_cmd_watchlist, chat, text, msg_from)
                    elif chat_auth and text == "/watchlistnotify":
                        self.bot_cmd_handler(self.bot_cmd_watchlistnotify, chat, text, msg_from)
                    elif chat_auth and text == "/overview":
                        self.bot_cmd_handler(self.bot_cmd_overview, chat, text, msg_from)
                    elif chat_auth and text == "/feargreed":
                        self.bot_cmd_handler(self.bot_cmd_feargreed, chat, text, msg_from)
                # Increase update id
                last_update_id = update_id + 1

def main():
    parser = argparse.ArgumentParser(description="Tickergram bot")
    parser.add_argument("token", help="Telegram Bot API token", nargs=1)
    parser.add_argument("-p", "--password", default="", help="Optional password needed to interact with the bot (enables the /auth command)")
    parser.add_argument("-r", "--redis", default="localhost", help="redis host to use")
    parser.add_argument("-l", "--port", type=int, default=6379, help="redis port to use")
    parser.add_argument("-d", "--db", type=int, default=0, help="redis database to use")
    args = parser.parse_args()

    b = tickergram(args.token[0], redis_host=args.redis, redis_port=args.port, redis_db=args.db, password=args.password)
    b.bot_loop()

def notify_watchers():
    parser = argparse.ArgumentParser(description="Tickergram bot notifications. Sends a message with the current status of the watchlist to the chats with enabled notifications.")
    parser.add_argument("token", help="Telegram Bot API token", nargs=1)
    parser.add_argument("-r", "--redis", default="localhost", help="redis host to use")
    parser.add_argument("-l", "--port", type=int, default=6379, help="redis port to use")
    parser.add_argument("-d", "--db", type=int, default=0, help="redis database to use")
    args = parser.parse_args()

    b = tickergram(args.token[0], redis_host=args.redis, redis_port=args.port, redis_db=args.db)
    b.bot_watchlist_notify()

if __name__ == "__main__":
    main()

