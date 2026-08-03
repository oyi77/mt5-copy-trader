//+------------------------------------------------------------------+
//|                                        TradeSender.mq5           |
//|          Master-side EA signal emitter — file-based relay        |
//|                                                                  |
//|  Run this EA on any chart of the MASTER terminal. Every poll     |
//|  interval it diffs the account's positions and pending orders    |
//|  against the previous snapshot and appends one pipe-delimited    |
//|  line per detected change to MQL5\Files\master_signals.txt.      |
//|                                                                  |
//|  The Python bridge (run.py with master.ea_signals_file set)      |
//|  tails that file — no MetaTrader5 package and no IPC (-6)        |
//|  handshake needed on the master side either.                    |
//|                                                                  |
//|  Line format (ANSI, SEQ strictly increasing):                    |
//|    SEQ|<n>|OPEN|ticket|symbol|ptype|volume|price|sl|tp|comment|magic
//|    SEQ|<n>|CLOSE|ticket|symbol|ptype|volume|price|sl|tp|comment|magic
//|    SEQ|<n>|MODIFY|ticket|symbol|ptype|volume|price|sl|tp|comment|magic|prev_volume
//|    SEQ|<n>|PLACE|ticket|symbol|otype|volume|price|sl|tp|expiration|comment|magic
//|    SEQ|<n>|DELETE|ticket|symbol|otype|volume|price|sl|tp|expiration|comment|magic
//|    SEQ|<n>|MODIFY_ORDER|ticket|symbol|otype|volume|price|sl|tp|expiration|comment|magic|prev_volume
//|    SEQ|<n>|STATUS|login|name|balance|equity|margin|margin_free|leverage|currency|server
//|    SEQ|<n>|HEARTBEAT|<unix_seconds>
//|                                                                  |
//|  The trailing |prev_volume is emitted only when the volume       |
//|  changed (partial close / partial fill).                         |
//|                                                                  |
//|  String fields (symbol, comment, account name, currency, server) |
//|  are escaped before writing: '\' becomes '\\' and '|' becomes    |
//|  '\|' — so no field content can break the pipe-delimited format. |
//|  The Python bridge (src/master_ea.py) unescapes on read.         |
//+------------------------------------------------------------------+
#property copyright "Copy Trade Engine"
#property version   "1.10"
#property strict

input int    PollIntervalMS       = 500;     // Poll interval (ms)
input int    HeartbeatIntervalMS  = 10000;   // STATUS/HEARTBEAT interval (ms)
input string SignalFile           = "master_signals.txt"; // File in MQL5\Files
input long   MaxFileBytes         = 5242880; // Rotate signal file above this size

//+------------------------------------------------------------------+
//| Position record                                                   |
//+------------------------------------------------------------------+
struct PosRec {
   ulong  ticket;
   string sym;
   int    type;
   double vol;
   double sl;
   double tp;
   string comment;
   int    magic;
   double price_open;
   double price_cur;
   string sig;   // comparison signature (excludes volatile price_cur)
};

//+------------------------------------------------------------------+
//| Pending order record                                              |
//+------------------------------------------------------------------+
struct OrderRec {
   ulong  ticket;
   string sym;
   int    type;
   double vol;
   double price;
   double sl;
   double tp;
   long   expiration;
   string comment;
   int    magic;
   string sig;   // comparison signature
};

PosRec   g_pos[];
OrderRec g_ord[];
ulong    g_seq = 0;
bool     g_first_pos = true;
bool     g_first_ord = true;
datetime g_last_heartbeat = 0;

//+------------------------------------------------------------------+
//| Emit helpers                                                      |
//+------------------------------------------------------------------+
string FmtD(double v)  { return DoubleToString(v, 8); }
string FmtV(double v)  { return DoubleToString(v, 2); }

// Escape a string field for the pipe-delimited format: '\' -> '\\' and
// '|' -> '\|'. Applied to every string field (symbol, comment, account
// name, currency, server) so no field content can break the format.
string EscapeField(string s) {
   StringReplace(s, "\\", "\\\\");
   StringReplace(s, "|", "\\|");
   return s;
}

string SeqLine(string action, string body) {
   g_seq++;
   return "SEQ|" + (string)g_seq + "|" + action + "|" + body;
}

bool WriteSignal(string line) {
   // MQL5 FileOpen only accepts paths relative to MQL5\Files\ — an
   // absolute path is rejected (ERR_FILE_NOT_FOUND). The data-folder
   // path is implicit; the file lands in <TERMINAL_DATA_PATH>\MQL5\Files\.
   string path = SignalFile;
   if (FileIsExist(path)) {
      // FileSize() needs a handle; open a probe to check the size.
      int probe = FileOpen(path, FILE_READ|FILE_TXT|FILE_ANSI);
      if (probe != INVALID_HANDLE) {
         ulong sz = FileSize(probe);
         FileClose(probe);
         if (sz > (ulong)MaxFileBytes) {
            Print("Rotating signal file (", (string)sz, " bytes)");
            FileDelete(path);
         }
      }
   }
   int h = FileOpen(path, FILE_READ|FILE_WRITE|FILE_TXT|FILE_ANSI);
   if (h == INVALID_HANDLE) {
      Print("WriteSignal: cannot open ", path, " err=", (string)GetLastError());
      return false;
   }
   FileSeek(h, 0, SEEK_END);
   FileWriteString(h, line + "\r\n");
   FileClose(h);
   return true;
}

void Emit(string action, string body) {
   string line = SeqLine(action, body);
   if (WriteSignal(line))
      Print("SIGNAL: ", line);
}

string PosBody(PosRec &r, double price, string prev_vol = "") {
   return (string)r.ticket + "|" + EscapeField(r.sym) + "|" + (string)r.type + "|"
          + FmtV(r.vol) + "|" + FmtD(price) + "|"
          + FmtD(r.sl) + "|" + FmtD(r.tp) + "|"
          + EscapeField(r.comment) + "|" + (string)r.magic + prev_vol;
}

string OrderBody(OrderRec &r, string prev_vol = "") {
   return (string)r.ticket + "|" + EscapeField(r.sym) + "|" + (string)r.type + "|"
          + FmtV(r.vol) + "|" + FmtD(r.price) + "|"
          + FmtD(r.sl) + "|" + FmtD(r.tp) + "|"
          + (string)r.expiration + "|"
          + EscapeField(r.comment) + "|" + (string)r.magic + prev_vol;
}

void EmitStatus() {
   string body = (string)AccountInfoInteger(ACCOUNT_LOGIN) + "|"
               + EscapeField(AccountInfoString(ACCOUNT_NAME)) + "|"
               + FmtV(AccountInfoDouble(ACCOUNT_BALANCE)) + "|"
               + FmtV(AccountInfoDouble(ACCOUNT_EQUITY)) + "|"
               + FmtV(AccountInfoDouble(ACCOUNT_MARGIN)) + "|"
               + FmtV(AccountInfoDouble(ACCOUNT_MARGIN_FREE)) + "|"
               + (string)AccountInfoInteger(ACCOUNT_LEVERAGE) + "|"
               + EscapeField(AccountInfoString(ACCOUNT_CURRENCY)) + "|"
               + EscapeField(AccountInfoString(ACCOUNT_SERVER));
   Emit("STATUS", body);
   Emit("HEARTBEAT", (string)TimeLocal());
}

//+------------------------------------------------------------------+
//| Diff helpers                                                      |
//+------------------------------------------------------------------+
// ArrayCopy() cannot copy arrays of structs containing strings, so
// snapshot arrays are copied element-wise instead.
void CopyPosArr(PosRec &dst[], PosRec &src[]) {
   int n = ArraySize(src);
   ArrayResize(dst, n);
   for (int i = 0; i < n; i++) dst[i] = src[i];
}

void CopyOrdArr(OrderRec &dst[], OrderRec &src[]) {
   int n = ArraySize(src);
   ArrayResize(dst, n);
   for (int i = 0; i < n; i++) dst[i] = src[i];
}

int IndexPos(PosRec &arr[], ulong ticket) {
   for (int i = 0; i < ArraySize(arr); i++)
      if (arr[i].ticket == ticket) return i;
   return -1;
}

int IndexOrd(OrderRec &arr[], ulong ticket) {
   for (int i = 0; i < ArraySize(arr); i++)
      if (arr[i].ticket == ticket) return i;
   return -1;
}

//+------------------------------------------------------------------+
//| Position diff → OPEN / CLOSE / MODIFY events                     |
//+------------------------------------------------------------------+
void CollectPositions() {
   PosRec cur[];
   int total = PositionsTotal();
   ArrayResize(cur, total);
   int n = 0;
   for (int i = 0; i < total; i++) {
      ulong t = PositionGetTicket(i);
      if (t == 0 || !PositionSelectByTicket(t)) continue;
      cur[n].ticket     = t;
      cur[n].sym        = PositionGetString(POSITION_SYMBOL);
      cur[n].type       = (int)PositionGetInteger(POSITION_TYPE);
      cur[n].vol        = PositionGetDouble(POSITION_VOLUME);
      cur[n].sl         = PositionGetDouble(POSITION_SL);
      cur[n].tp         = PositionGetDouble(POSITION_TP);
      cur[n].comment    = PositionGetString(POSITION_COMMENT);
      cur[n].magic      = (int)PositionGetInteger(POSITION_MAGIC);
      cur[n].price_open = PositionGetDouble(POSITION_PRICE_OPEN);
      cur[n].price_cur  = PositionGetDouble(POSITION_PRICE_CURRENT);
      cur[n].sig        = cur[n].sym + "|" + (string)cur[n].type + "|"
                          + FmtV(cur[n].vol) + "|" + FmtD(cur[n].sl) + "|"
                          + FmtD(cur[n].tp) + "|" + cur[n].comment + "|"
                          + (string)cur[n].magic;
      n++;
   }
   ArrayResize(cur, n);

   if (g_first_pos) {
      // Baseline — never relay positions that existed before the EA attached
      CopyPosArr(g_pos, cur);
      g_first_pos = false;
      return;
   }

   // OPEN: in current, not in previous
   for (int i = 0; i < n; i++)
      if (IndexPos(g_pos, cur[i].ticket) < 0)
         Emit("OPEN", PosBody(cur[i], cur[i].price_open));

   // CLOSE: in previous, not in current (report last known price)
   for (int i = 0; i < ArraySize(g_pos); i++)
      if (IndexPos(cur, g_pos[i].ticket) < 0)
         Emit("CLOSE", PosBody(g_pos[i], g_pos[i].price_cur));

   // MODIFY: in both but signature changed
   for (int i = 0; i < ArraySize(g_pos); i++) {
      int j = IndexPos(cur, g_pos[i].ticket);
      if (j < 0) continue;
      if (cur[j].sig != g_pos[i].sig) {
         string prev_vol = "";
         if (cur[j].vol != g_pos[i].vol)
            prev_vol = "|" + FmtV(g_pos[i].vol);
         Emit("MODIFY", PosBody(cur[j], cur[j].price_open, prev_vol));
      }
   }

   CopyPosArr(g_pos, cur);
}

//+------------------------------------------------------------------+
//| Pending order diff → PLACE / DELETE / MODIFY_ORDER events        |
//+------------------------------------------------------------------+
void CollectOrders() {
   OrderRec cur[];
   int total = OrdersTotal();
   ArrayResize(cur, total);
   int n = 0;
   for (int i = 0; i < total; i++) {
      ulong t = OrderGetTicket(i);
      if (t == 0 || !OrderSelect(t)) continue;
      int ot = (int)OrderGetInteger(ORDER_TYPE);
      // Market orders (BUY=0, SELL=1) flash through the order pool while a
      // deal is being executed — they are never pending orders. Skipping them
      // prevents a spurious PLACE→DELETE pair per market execution.
      if (ot <= 1) continue;
      cur[n].ticket     = t;
      cur[n].sym        = OrderGetString(ORDER_SYMBOL);
      cur[n].type       = ot;
      cur[n].vol        = OrderGetDouble(ORDER_VOLUME_CURRENT);
      cur[n].price      = OrderGetDouble(ORDER_PRICE_OPEN);
      cur[n].sl         = OrderGetDouble(ORDER_SL);
      cur[n].tp         = OrderGetDouble(ORDER_TP);
      cur[n].expiration = (long)OrderGetInteger(ORDER_TIME_EXPIRATION);
      cur[n].comment    = OrderGetString(ORDER_COMMENT);
      cur[n].magic      = (int)OrderGetInteger(ORDER_MAGIC);
      cur[n].sig        = cur[n].sym + "|" + (string)cur[n].type + "|"
                          + FmtV(cur[n].vol) + "|" + FmtD(cur[n].price) + "|"
                          + FmtD(cur[n].sl) + "|" + FmtD(cur[n].tp) + "|"
                          + (string)cur[n].expiration + "|" + cur[n].comment + "|"
                          + (string)cur[n].magic;
      n++;
   }
   ArrayResize(cur, n);

   if (g_first_ord) {
      CopyOrdArr(g_ord, cur);
      g_first_ord = false;
      return;
   }

   for (int i = 0; i < n; i++)
      if (IndexOrd(g_ord, cur[i].ticket) < 0)
         Emit("PLACE", OrderBody(cur[i]));

   for (int i = 0; i < ArraySize(g_ord); i++)
      if (IndexOrd(cur, g_ord[i].ticket) < 0)
         Emit("DELETE", OrderBody(g_ord[i]));

   for (int i = 0; i < ArraySize(g_ord); i++) {
      int j = IndexOrd(cur, g_ord[i].ticket);
      if (j < 0) continue;
      if (cur[j].sig != g_ord[i].sig) {
         string prev_vol = "";
         if (cur[j].vol != g_ord[i].vol)
            prev_vol = "|" + FmtV(g_ord[i].vol);
         Emit("MODIFY_ORDER", OrderBody(cur[j], prev_vol));
      }
   }

   CopyOrdArr(g_ord, cur);
}

//+------------------------------------------------------------------+
//| Expert functions                                                  |
//+------------------------------------------------------------------+
int OnInit() {
   // SEQ base = unix seconds, so SEQ stays strictly increasing across EA
   // restarts. (If a restart happens within the same second, the bridge
   // re-anchors on the first HEARTBEAT it sees.)
   g_seq = (ulong)TimeLocal();
   EventSetMillisecondTimer(PollIntervalMS);
   Print("TradeSender EA started. Poll every ", (string)PollIntervalMS,
         "ms, seq base=", (string)g_seq,
         ", signal file=", SignalFile);
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason) {
   EventKillTimer();
   Print("TradeSender EA stopped.");
}

void OnTimer() {
   // Liveness + account status
   datetime now = TimeLocal();
   if (g_last_heartbeat == 0 || now - g_last_heartbeat >= HeartbeatIntervalMS / 1000) {
      EmitStatus();
      g_last_heartbeat = now;
   }

   CollectPositions();
   CollectOrders();
}
//+------------------------------------------------------------------+
