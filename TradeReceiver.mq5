//+------------------------------------------------------------------+
//|                                             TradeReceiver.mq5    |
//|                                 File-based trade relay EA        |
//|                                                                  |
//+------------------------------------------------------------------+
#property copyright "Copy Trade Engine"
#property version   "1.10"
#property strict

input int    TimerIntervalMS = 500;  // Poll interval (ms)
input int    MaxSlippage     = 50;   // Max slippage in points
input int    ExpertMagic     = 200001; // Magic number for trades
input string PendingFile     = "pending.txt";
input string ResultFile      = "result.txt";

//+------------------------------------------------------------------+
//| Error code to short string                                       |
//+------------------------------------------------------------------+
string ErrStr(int code) {
   switch(code) {
      case 0:    return "OK";
      case 4099: return "OFFLINE";
      case 4100: return "BROKER_BUSY";
      case 4101: return "TRADE_TIMEOUT";
      case 4104: return "MARKET_CLOSED";
      case 4105: return "PRICE_LIMIT";
      case 4106: return "REQUOTE";
      case 4107: return "INVALID_VOLUME";
      case 4108: return "INVALID_STOPS";
      case 4109: return "TRADE_DISABLED";
      case 4110: return "INVALID_PRICE";
      case 4111: return "INVALID_SLTP";
      case 4112: return "MODIFY_DENIED";
      case 4114: return "TOO_MANY";
      case 4115: return "CONTEXT_BUSY";
      case 4756: return "CLOSED";
      case 146:  return "BUSY";
      default:   return "ERR_" + (string)code;
   }
}

//+------------------------------------------------------------------+
//| Custom trade send with retry                                     |
//+------------------------------------------------------------------+
bool TradeSend(MqlTradeRequest &req, MqlTradeResult &res, int retries=3) {
   for (int i = 0; i < retries; i++) {
      ResetLastError();
      if (OrderSend(req, res)) return true;
      int err = GetLastError();
      Print("OrderSend attempt ", (string)(i+1), " failed: err=", (string)err, " ", ErrStr(err));
      if (err == 4756 || err == 146) { Sleep(1000 * (i+1)); continue; }
      break;
   }
   return false;
}

//+------------------------------------------------------------------+
//| Find position by comment text                                    |
//+------------------------------------------------------------------+
ulong FindPosByComment(string comment) {
   for (int i = PositionsTotal() - 1; i >= 0; i--) {
      ulong t = PositionGetTicket(i);
      if (t > 0 && PositionSelectByTicket(t)) {
         if (PositionGetString(POSITION_COMMENT) == comment) return t;
      }
   }
   return 0;
}

//+------------------------------------------------------------------+
//| Find pending order by comment text                               |
//+------------------------------------------------------------------+
ulong FindOrderByComment(string comment) {
   for (int i = OrdersTotal() - 1; i >= 0; i--) {
      ulong t = OrderGetTicket(i);
      if (t > 0 && OrderSelect(t)) {
         if (OrderGetString(ORDER_COMMENT) == comment) return t;
      }
   }
   return 0;
}

//+------------------------------------------------------------------+
//| Open market position                                             |
//+------------------------------------------------------------------+
string OpenPosition(string symbol, int cmd, double vol, double sl, double tp, string comment) {
   double bid, ask;
   if (!SymbolInfoDouble(symbol, SYMBOL_BID, bid) ||
       !SymbolInfoDouble(symbol, SYMBOL_ASK, ask))
      return "FAILED|PRICE";

   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   MqlTradeRequest req = {};
   MqlTradeResult res = {};
   req.action = TRADE_ACTION_DEAL;
   req.symbol = symbol;
   req.volume = vol;
   req.deviation = MaxSlippage;
   req.comment = comment;
   req.magic = ExpertMagic;
   req.type = (cmd == 0) ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   req.price = (cmd == 0) ? ask : bid;
   req.sl = (sl > 0) ? NormalizeDouble(sl, digits) : 0;
   req.tp = (tp > 0) ? NormalizeDouble(tp, digits) : 0;

   if (TradeSend(req, res))
      return "DONE|" + (string)res.order;
   else
      return "FAILED|" + (string)GetLastError();
}

//+------------------------------------------------------------------+
//| Close position by ticket                                         |
//+------------------------------------------------------------------+
string ClosePosition(ulong ticket) {
   if (!PositionSelectByTicket(ticket)) return "FAILED|NF";
   ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   double vol = PositionGetDouble(POSITION_VOLUME);
   string sym = PositionGetString(POSITION_SYMBOL);

   double bid, ask;
   if (!SymbolInfoDouble(sym, SYMBOL_BID, bid) ||
       !SymbolInfoDouble(sym, SYMBOL_ASK, ask))
      return "FAILED|PRICE";

   MqlTradeRequest req = {};
   MqlTradeResult res = {};
   req.action = TRADE_ACTION_DEAL;
   req.symbol = sym;
   req.volume = vol;
   req.position = ticket;
   req.deviation = MaxSlippage;
   if (type == POSITION_TYPE_BUY) { req.type = ORDER_TYPE_SELL; req.price = bid; }
   else { req.type = ORDER_TYPE_BUY; req.price = ask; }

   if (TradeSend(req, res))
      return "DONE|0";
   else
      return "FAILED|" + (string)GetLastError();
}

//+------------------------------------------------------------------+
//| Modify SL/TP of an open position                                 |
//+------------------------------------------------------------------+
string ModifyPosition(ulong ticket, double sl, double tp) {
   if (!PositionSelectByTicket(ticket)) return "FAILED|NF";
   string sym = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

   MqlTradeRequest req = {};
   MqlTradeResult res = {};
   req.action = TRADE_ACTION_SLTP;
   req.symbol = sym;
   req.position = ticket;
   req.sl = (sl > 0) ? NormalizeDouble(sl, digits) : 0;
   req.tp = (tp > 0) ? NormalizeDouble(tp, digits) : 0;

   if (TradeSend(req, res))
      return "DONE|0";
   else
      return "FAILED|" + (string)GetLastError();
}

//+------------------------------------------------------------------+
//| Place a pending order (limit/stop)                                |
//+------------------------------------------------------------------+
string PlacePending(string symbol, int otype, double vol, double price,
                    double sl, double tp, long expiration, string comment) {
   // Replay safety: a re-broadcast PLACE must not create a duplicate.
   ulong existing = FindOrderByComment(comment);
   if (existing > 0) return "DONE|" + (string)existing;

   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   MqlTradeRequest req = {};
   MqlTradeResult res = {};
   req.action = TRADE_ACTION_PENDING;
   req.symbol = symbol;
   req.volume = vol;
   req.type = (ENUM_ORDER_TYPE)otype;  // 2..7: *_LIMIT / *_STOP / *_STOP_LIMIT
   req.price = NormalizeDouble(price, digits);
   req.sl = (sl > 0) ? NormalizeDouble(sl, digits) : 0;
   req.tp = (tp > 0) ? NormalizeDouble(tp, digits) : 0;
   req.comment = comment;
   req.magic = ExpertMagic;
   req.type_time = (expiration > 0) ? ORDER_TIME_SPECIFIED : ORDER_TIME_GTC;
   req.type_filling = ORDER_FILLING_RETURN;
   if (expiration > 0) req.expiration = (datetime)expiration;

   if (TradeSend(req, res))
      return "DONE|" + (string)res.order;
   else
      return "FAILED|" + (string)GetLastError();
}

//+------------------------------------------------------------------+
//| Modify price/SL/TP/expiration of a pending order                 |
//+------------------------------------------------------------------+
string ModifyPending(ulong ticket, double price, double sl, double tp,
                     long expiration) {
   if (!OrderSelect(ticket)) return "FAILED|NF";
   string sym = OrderGetString(ORDER_SYMBOL);
   int digits = (int)SymbolInfoInteger(sym, SYMBOL_DIGITS);

   MqlTradeRequest req = {};
   MqlTradeResult res = {};
   req.action = TRADE_ACTION_MODIFY;
   req.order = ticket;
   req.symbol = sym;
   req.volume = OrderGetDouble(ORDER_VOLUME_CURRENT);  // volume is read-only for MODIFY
   req.price = NormalizeDouble(price, digits);
   req.sl = (sl > 0) ? NormalizeDouble(sl, digits) : 0;
   req.tp = (tp > 0) ? NormalizeDouble(tp, digits) : 0;
   req.magic = ExpertMagic;
   req.type_time = (expiration > 0) ? ORDER_TIME_SPECIFIED : ORDER_TIME_GTC;
   req.type_filling = ORDER_FILLING_RETURN;
   if (expiration > 0) req.expiration = (datetime)expiration;

   if (TradeSend(req, res))
      return "DONE|0";
   else
      return "FAILED|" + (string)GetLastError();
}

//+------------------------------------------------------------------+
//| Delete a pending order                                            |
//+------------------------------------------------------------------+
string DeletePending(ulong ticket) {
   if (!OrderSelect(ticket)) return "FAILED|NF";
   MqlTradeRequest req = {};
   MqlTradeResult res = {};
   req.action = TRADE_ACTION_REMOVE;
   req.order = ticket;
   req.symbol = OrderGetString(ORDER_SYMBOL);
   req.magic = ExpertMagic;

   if (TradeSend(req, res))
      return "DONE|0";
   else
      return "FAILED|" + (string)GetLastError();
}

//+------------------------------------------------------------------+
//| Execute a command line                                           |
//+------------------------------------------------------------------+
string Exec(string cmd) {
   string p[16];
   int n = StringSplit(cmd, '|', p);
   if (n < 1) return "FAILED|EMPTY";
   string a = p[0];
   string sym; double vol, sl, tp; ulong ticket;

   if (a == "OPEN_BUY" || a == "OPEN_SELL") {
      if (n < 3) return "FAILED|PARAMS";
      sym = p[1];
      vol = StringToDouble(p[2]);
      sl  = (n >= 4 && p[3] != "") ? StringToDouble(p[3]) : 0;
      tp  = (n >= 5 && p[4] != "") ? StringToDouble(p[4]) : 0;
      ticket = (n >= 6 && p[5] != "") ? (ulong)StringToInteger(p[5]) : 0;
      string comment = (ticket > 0) ? "copied_" + (string)ticket : "cpy";
      int cmdtype = (a == "OPEN_BUY") ? 0 : 1;
      return OpenPosition(sym, cmdtype, vol, sl, tp, comment);
   }

   if (a == "CLOSE") {
      if (n < 2) return "FAILED|TICKET";
      ticket = (ulong)StringToInteger(p[1]);
      ulong found = FindPosByComment("copied_" + (string)ticket);
      if (found == 0) return "FAILED|NF_COMMENT";
      return ClosePosition(found);
   }

   if (a == "MODIFY") {
      // MODIFY|symbol|volume|sl|tp|ticket  (SL/TP in the same slots as
      // OPEN_BUY so the Python relay builds one command format)
      if (n < 6) return "FAILED|PARAMS";
      ticket = (ulong)StringToInteger(p[5]);
      double nsl = (n >= 4 && p[3] != "") ? StringToDouble(p[3]) : 0;
      double ntp = (n >= 5 && p[4] != "") ? StringToDouble(p[4]) : 0;
      ulong found = FindPosByComment("copied_" + (string)ticket);
      if (found == 0) return "FAILED|NF_COMMENT";
      return ModifyPosition(found, nsl, ntp);
   }

   if (a == "PLACE_ORDER") {
      // PLACE_ORDER|symbol|otype|volume|price|sl|tp|expiration|ticket
      if (n < 9) return "FAILED|PARAMS";
      sym = p[1];
      int otype = (int)StringToInteger(p[2]);
      vol = StringToDouble(p[3]);
      double price = StringToDouble(p[4]);
      sl  = (n >= 6 && p[5] != "") ? StringToDouble(p[5]) : 0;
      tp  = (n >= 7 && p[6] != "") ? StringToDouble(p[6]) : 0;
      long expiration = (n >= 8 && p[7] != "") ? StringToInteger(p[7]) : 0;
      ticket = (n >= 9 && p[8] != "") ? (ulong)StringToInteger(p[8]) : 0;
      string pcomment = (ticket > 0) ? "copied_" + (string)ticket : "cpy";
      return PlacePending(sym, otype, vol, price, sl, tp, expiration, pcomment);
   }

   if (a == "MODIFY_ORDER") {
      // MODIFY_ORDER|symbol|otype|volume|price|sl|tp|expiration|ticket
      if (n < 9) return "FAILED|PARAMS";
      ticket = (ulong)StringToInteger(p[8]);
      ulong found = FindOrderByComment("copied_" + (string)ticket);
      if (found == 0) return "FAILED|NF_COMMENT";
      double price = StringToDouble(p[4]);
      double nsl  = (p[5] != "") ? StringToDouble(p[5]) : 0;
      double ntp  = (p[6] != "") ? StringToDouble(p[6]) : 0;
      long expiration = (p[7] != "") ? StringToInteger(p[7]) : 0;
      return ModifyPending(found, price, nsl, ntp, expiration);
   }

   if (a == "DELETE_ORDER") {
      if (n < 2) return "FAILED|TICKET";
      ticket = (ulong)StringToInteger(p[1]);
      ulong found = FindOrderByComment("copied_" + (string)ticket);
      if (found == 0) return "FAILED|NF_COMMENT";
      return DeletePending(found);
   }

   if (a == "CLOSE_ALL") {
      int closed = 0, total = PositionsTotal();
      for (int i = total - 1; i >= 0; i--) {
         ulong t = PositionGetTicket(i);
         if (t > 0 && PositionSelectByTicket(t)) {
            string r = ClosePosition(t);
            if (StringFind(r, "DONE") >= 0) closed++;
         }
      }
      return "DONE|" + (string)closed + "_OK";
   }

   if (a == "PING") return "DONE|PONG";
   return "FAILED|UNKNOWN";
}

//+------------------------------------------------------------------+
//| Expert initialization                                            |
//+------------------------------------------------------------------+
int OnInit() {
   EventSetMillisecondTimer(TimerIntervalMS);
   Print("TradeReceiver EA started. Poll every ", (string)TimerIntervalMS, "ms");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
   EventKillTimer();
   Print("TradeReceiver EA stopped.");
}

//+------------------------------------------------------------------+
//| Timer: poll for pending commands                                 |
//+------------------------------------------------------------------+
void OnTimer() {
   // MQL5 FileOpen/FileIsExist only accept paths relative to MQL5\Files\ —
   // absolute paths (e.g. from TERMINAL_DATA_PATH) are rejected with
   // ERR_FILE_NOT_FOUND. The data-folder path is implicit: commands land in
   // <TERMINAL_DATA_PATH>\MQL5\Files\pending.txt and results in result.txt,
   // the same convention TradeSender.mq5 uses for the signal file.
   string pp = PendingFile;
   string rp = ResultFile;

   if (!FileIsExist(pp)) return;

   int h = FileOpen(pp, FILE_READ|FILE_TXT|FILE_ANSI);
   if (h == INVALID_HANDLE) {
      Print("Receiver: cannot open ", pp, " err=", (string)GetLastError());
      FileDelete(pp);
      return;
   }

   string cmd = "";
   while (!FileIsEnding(h)) cmd += FileReadString(h);
   FileClose(h);
   FileDelete(pp);

   StringTrimRight(cmd);
   StringTrimLeft(cmd);
   if (cmd == "") return;

   Print("CMD: ", cmd);
   string result = Exec(cmd);
   Print("RES: ", result);

   h = FileOpen(rp, FILE_WRITE|FILE_TXT|FILE_ANSI);
   if (h != INVALID_HANDLE) {
      FileWriteString(h, result);
      FileClose(h);
   } else {
      Print("Receiver: cannot write ", rp, " err=", (string)GetLastError());
   }
}
//+------------------------------------------------------------------+
