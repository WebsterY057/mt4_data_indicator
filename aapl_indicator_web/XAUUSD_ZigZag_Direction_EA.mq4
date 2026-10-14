#property strict
#property version   "1.00"

input ENUM_TIMEFRAMES SignalTF = PERIOD_M1;
input double Lots = 0.01;
input int MagicNumber = 26082601;
input int SlippagePoints = 30;
input int ZigZagDepth = 30;
input int ZigZagDeviation = 5;
input int ZigZagBackstep = 3;
input int ZigZagLookbackBars = 500;
input bool LeftOnlyZigZag = true;
input double SarStep = 0.016;
input double SarMaximum = 0.20;
input int HaSmmaPeriod = 6;
input int HaLwmaPeriod = 2;
input int SarLegLookbackBars = 300;
input double RewardRisk = 2.0;
input bool UseServerTimeBlackout = true;
input int BlackoutStartMinute = 1200;
input int BlackoutEndMinute = 1260;

datetime lastBarTime = 0;

int OnInit() { return(INIT_SUCCEEDED); }

void OnTick() {
   if(iTime(Symbol(), SignalTF, 0) == lastBarTime) return;
   lastBarTime = iTime(Symbol(), SignalTF, 0);
   if(CountOpenPositions() > 0 || IsBlackout()) return;
   EvaluateClosedBar();
}

void EvaluateClosedBar() {
   const int signal = 1;
   if(iBars(Symbol(), SignalTF) < ZigZagLookbackBars + ZigZagDepth + 10) return;
   bool sarLong = iSAR(Symbol(), SignalTF, SarStep, SarMaximum, signal) < iClose(Symbol(), SignalTF, signal);
   int legStart = FindSarReversal(sarLong);
   if(legStart < 3 || !PriorLegAllows(sarLong)) return;
   if(!DirectionFilterAllows(sarLong) || !SmoothedHAAllows(signal, sarLong)) return;
   if(!PullbackComplete(signal, legStart, sarLong)) return;

   double stop = SarSegmentStop(legStart, sarLong);
   double entry = sarLong ? Ask : Bid;
   if((sarLong && stop >= entry) || (!sarLong && stop <= entry)) return;
   double target = sarLong ? entry + RewardRisk * (entry - stop) : entry - RewardRisk * (stop - entry);
   int digits = (int)MarketInfo(Symbol(), MODE_DIGITS);
   entry = NormalizeDouble(entry, digits);
   stop = NormalizeDouble(stop, digits);
   target = NormalizeDouble(target, digits);
   RefreshRates();
   int ticket = OrderSend(Symbol(), sarLong ? OP_BUY : OP_SELL, Lots, sarLong ? Ask : Bid,
                          SlippagePoints, stop, target, "ZZ directional entry", MagicNumber, 0,
                          sarLong ? clrLime : clrTomato);
   if(ticket < 0) Print("OrderSend failed: ", GetLastError());
}

int CountOpenPositions() {
   int count = 0;
   for(int i = OrdersTotal() - 1; i >= 0; i--) {
      if(OrderSelect(i, SELECT_BY_POS, MODE_TRADES) && OrderSymbol() == Symbol() && OrderMagicNumber() == MagicNumber)
         count++;
   }
   return count;
}

bool IsBlackout() {
   if(!UseServerTimeBlackout) return false;
   datetime now = TimeCurrent();
   int minute = TimeHour(now) * 60 + TimeMinute(now);
   if(BlackoutStartMinute <= BlackoutEndMinute)
      return minute >= BlackoutStartMinute && minute < BlackoutEndMinute;
   return minute >= BlackoutStartMinute || minute < BlackoutEndMinute;
}

double ZigZagValue(int shift) {
   return iCustom(Symbol(), SignalTF, "ZigZag", ZigZagDepth, ZigZagDeviation, ZigZagBackstep, 0, shift);
}

bool IsHighPivot(int shift, double value) { return MathAbs(value - iHigh(Symbol(), SignalTF, shift)) <= Point * 2.0; }

bool LatestPivots(bool high, double &latest, double &previous) {
   int found = 0;
   for(int shift = ZigZagDepth + 1; shift <= ZigZagLookbackBars; shift++) {
      double value = ZigZagValue(shift);
      if(value == 0.0 || IsHighPivot(shift, value) != high) continue;
      if(found == 0) { latest = value; found++; }
      else { previous = value; return true; }
   }
   return false;
}

void AddLeftPivot(int type, double value, int shift, int &count, int &types[], double &values[], int &shifts[]) {
   if(count == 0) { types[0] = type; values[0] = value; shifts[0] = shift; count = 1; return; }
   int last = count - 1;
   if(MathAbs(value - values[last]) < ZigZagDeviation * Point) return;
   if(type == types[last]) {
      bool extreme = type > 0 ? value > values[last] : value < values[last];
      if(extreme || shifts[last] - shift <= ZigZagBackstep) { values[last] = value; shifts[last] = shift; }
      return;
   }
   types[count] = type; values[count] = value; shifts[count] = shift; count++;
}

bool LeftOnlyState(int &direction, int &lastType) {
   direction = 0; lastType = 0;
   int limit = MathMin(ZigZagLookbackBars, iBars(Symbol(), SignalTF) - 2);
   if(limit <= ZigZagDepth + 2) return false;
   int types[], shifts[], count = 0; double values[];
   ArrayResize(types, limit * 2); ArrayResize(shifts, limit * 2); ArrayResize(values, limit * 2);
   for(int shift = limit; shift >= 1; shift--) {
      double high = iHigh(Symbol(), SignalTF, shift), low = iLow(Symbol(), SignalTF, shift);
      bool isHigh = true, isLow = true;
      int oldest = MathMin(limit, shift + ZigZagDepth);
      for(int older = shift + 1; older <= oldest; older++) {
         if(iHigh(Symbol(), SignalTF, older) > high) isHigh = false;
         if(iLow(Symbol(), SignalTF, older) < low) isLow = false;
         if(!isHigh && !isLow) break;
      }
      if(isHigh) AddLeftPivot(1, high, shift, count, types, values, shifts);
      if(isLow) AddLeftPivot(-1, low, shift, count, types, values, shifts);
   }
   if(count < 2) return false;
   int last = count - 1; lastType = types[last];
   for(int i = last - 1; i >= 0; i--) {
      if(types[i] != lastType) continue;
      direction = values[last] > values[i] ? 1 : values[last] < values[i] ? -1 : 0;
      return direction != 0;
   }
   return false;
}

bool DirectionFilterAllows(bool isLong) {
   int direction, lastType;
   if(!LeftOnlyState(direction, lastType)) return false;
   return isLong ? direction > 0 : direction < 0;
}

bool PriorLegAllows(bool isLong) {
   int direction, lastType;
   if(!LeftOnlyState(direction, lastType)) return false;
   return isLong ? lastType > 0 : lastType < 0;
}

int FindSarReversal(bool isLong) {
   for(int shift = 2; shift <= SarLegLookbackBars; shift++) {
      bool side = iSAR(Symbol(), SignalTF, SarStep, SarMaximum, shift) < iClose(Symbol(), SignalTF, shift);
      bool olderSide = iSAR(Symbol(), SignalTF, SarStep, SarMaximum, shift + 1) < iClose(Symbol(), SignalTF, shift + 1);
      if(side == isLong && olderSide != isLong) return shift;
   }
   return -1;
}

double SarSegmentStop(int legStart, bool isLong) {
   double stop = isLong ? DBL_MAX : -DBL_MAX;
   // Signal is shift 1. Initial stop uses the SAR segment only through the preceding completed bar.
   for(int shift = legStart; shift >= 2; shift--) {
      if(isLong) stop = MathMin(stop, iLow(Symbol(), SignalTF, shift));
      else stop = MathMax(stop, iHigh(Symbol(), SignalTF, shift));
   }
   return stop;
}

bool SmoothedHAAllows(int signal, bool isLong) {
   int count = MathMax(HaLwmaPeriod + 2, 80);
   double rawOpen[], rawClose[];
   ArrayResize(rawOpen, count);
   ArrayResize(rawClose, count);
   for(int n = count - 1; n >= 0; n--) {
      int shift = signal + n;
      double o = iMA(Symbol(), SignalTF, HaSmmaPeriod, 0, MODE_SMMA, PRICE_OPEN, shift);
      double h = iMA(Symbol(), SignalTF, HaSmmaPeriod, 0, MODE_SMMA, PRICE_HIGH, shift);
      double l = iMA(Symbol(), SignalTF, HaSmmaPeriod, 0, MODE_SMMA, PRICE_LOW, shift);
      double c = iMA(Symbol(), SignalTF, HaSmmaPeriod, 0, MODE_SMMA, PRICE_CLOSE, shift);
      rawClose[n] = (o + h + l + c) / 4.0;
      rawOpen[n] = (n == count - 1) ? (o + c) / 2.0 : (rawOpen[n + 1] + rawClose[n + 1]) / 2.0;
   }
   double open = 0.0, close = 0.0, weight = 0.0;
   for(int n = 0; n < HaLwmaPeriod; n++) {
      double w = HaLwmaPeriod - n;
      open += rawOpen[n] * w;
      close += rawClose[n] * w;
      weight += w;
   }
   return isLong ? close / weight > open / weight : close / weight < open / weight;
}

bool PullbackComplete(int signal, int legStart, bool isLong) {
   if(isLong) {
      if(iClose(Symbol(), SignalTF, signal) <= iOpen(Symbol(), SignalTF, signal)) return false;
      for(int rebound = signal + 1; rebound < legStart; rebound++) {
         if(iHigh(Symbol(), SignalTF, signal) <= iHigh(Symbol(), SignalTF, rebound) || iLow(Symbol(), SignalTF, signal) < iLow(Symbol(), SignalTF, rebound)) continue;
         if(iHigh(Symbol(), SignalTF, rebound) < iHigh(Symbol(), SignalTF, legStart) && iLow(Symbol(), SignalTF, rebound) <= iLow(Symbol(), SignalTF, legStart)) return true;
         for(int base = rebound + 1; base < legStart; base++) {
            bool validBase = iHigh(Symbol(), SignalTF, base) > iHigh(Symbol(), SignalTF, legStart) || SmoothedHAAllows(base, true);
            if(validBase && iHigh(Symbol(), SignalTF, rebound) < iHigh(Symbol(), SignalTF, base) && iLow(Symbol(), SignalTF, rebound) <= iLow(Symbol(), SignalTF, base)) return true;
         }
      }
      return false;
   }
   if(iClose(Symbol(), SignalTF, signal) >= iOpen(Symbol(), SignalTF, signal)) return false;
   for(int rebound = signal + 1; rebound < legStart; rebound++) {
      if(iHigh(Symbol(), SignalTF, signal) > iHigh(Symbol(), SignalTF, rebound) || iLow(Symbol(), SignalTF, signal) >= iLow(Symbol(), SignalTF, rebound)) continue;
      if(iHigh(Symbol(), SignalTF, rebound) >= iHigh(Symbol(), SignalTF, legStart) && iLow(Symbol(), SignalTF, rebound) > iLow(Symbol(), SignalTF, legStart)) return true;
      for(int base = rebound + 1; base < legStart; base++) {
         bool validBase = iLow(Symbol(), SignalTF, base) < iLow(Symbol(), SignalTF, legStart) || SmoothedHAAllows(base, false);
         if(validBase && iHigh(Symbol(), SignalTF, rebound) >= iHigh(Symbol(), SignalTF, base) && iLow(Symbol(), SignalTF, rebound) > iLow(Symbol(), SignalTF, base)) return true;
      }
   }
   return false;
}
