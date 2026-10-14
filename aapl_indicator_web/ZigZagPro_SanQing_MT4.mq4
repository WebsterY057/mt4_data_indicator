//+------------------------------------------------------------------+
//| ZigZagPro_SanQing_MT4.mq4                                        |
//| Causal/left-confirmed ZigZag used by the current web visualizer. |
//| Default main line: Depth 30, Deviation 5, Backstep 3.             |
//+------------------------------------------------------------------+
#property strict
#property indicator_chart_window
#property indicator_buffers 3

#property indicator_color1 clrGold
#property indicator_color2 clrLime
#property indicator_color3 clrTomato
#property indicator_width1 2
#property indicator_width2 1
#property indicator_width3 1

input int    Depth          = 30;
input double Deviation      = 5.0;
input int    Backstep       = 3;
input bool   LeftOnly       = true;   // true = no future bars; matches web version
input bool   ShowPivotDots  = false;

double ZigZagBuffer[];
double HighPivotBuffer[];
double LowPivotBuffer[];

int PivotIndex[];
double PivotValue[];
int PivotType[]; // 1 high, -1 low

void ClearBuffers(const int total)
{
   for(int i = 0; i < total; i++)
   {
      ZigZagBuffer[i] = EMPTY_VALUE;
      HighPivotBuffer[i] = EMPTY_VALUE;
      LowPivotBuffer[i] = EMPTY_VALUE;
   }
}

void AddOrReplacePivot(
   int &count,
   const int index,
   const double value,
   const int type,
   const int backstep
)
{
   if(count <= 0)
   {
      PivotIndex[count] = index;
      PivotValue[count] = value;
      PivotType[count] = type;
      count++;
      return;
   }

   int last = count - 1;
   if(PivotType[last] == type)
   {
      bool more_extreme = (type == 1 && value > PivotValue[last]) || (type == -1 && value < PivotValue[last]);
      bool within_backstep = MathAbs(index - PivotIndex[last]) <= backstep;
      if(more_extreme || within_backstep)
      {
         PivotIndex[last] = index;
         PivotValue[last] = value;
         PivotType[last] = type;
      }
      return;
   }

   PivotIndex[count] = index;
   PivotValue[count] = value;
   PivotType[count] = type;
   count++;
}

int OnInit()
{
   IndicatorShortName("ZigZag Pro SanQing");

   IndicatorBuffers(3);
   SetIndexBuffer(0, ZigZagBuffer, INDICATOR_DATA);
   SetIndexBuffer(1, HighPivotBuffer, INDICATOR_DATA);
   SetIndexBuffer(2, LowPivotBuffer, INDICATOR_DATA);

   SetIndexStyle(0, DRAW_SECTION, STYLE_SOLID, 2, clrGold);
   SetIndexStyle(1, ShowPivotDots ? DRAW_ARROW : DRAW_NONE, STYLE_SOLID, 1, clrLime);
   SetIndexStyle(2, ShowPivotDots ? DRAW_ARROW : DRAW_NONE, STYLE_SOLID, 1, clrTomato);

   SetIndexArrow(1, 159);
   SetIndexArrow(2, 159);

   SetIndexLabel(0, "ZigZag");
   SetIndexLabel(1, "High Pivot");
   SetIndexLabel(2, "Low Pivot");

   IndicatorDigits(Digits);
   return INIT_SUCCEEDED;
}

int OnCalculate(
   const int rates_total,
   const int prev_calculated,
   const datetime &time[],
   const double &open[],
   const double &high[],
   const double &low[],
   const double &close[],
   const long &tick_volume[],
   const long &volume[],
   const int &spread[]
)
{
   if(rates_total <= 0)
      return 0;

   int depth = MathMax(1, Depth);
   int backstep = MathMax(0, Backstep);
   double min_move = MathMax(0.0, Deviation) * Point;

   ArrayResize(PivotIndex, rates_total);
   ArrayResize(PivotValue, rates_total);
   ArrayResize(PivotType, rates_total);
   ClearBuffers(rates_total);

   int pivot_count = 0;

   // MT4 arrays are series: 0 is current, rates_total-1 is oldest.
   // Loop from oldest to newest so pivot order matches the web code.
   for(int i = rates_total - 1; i >= 0; i--)
   {
      int older_end = MathMin(rates_total - 1, i + depth);
      int newer_end = LeftOnly ? i : MathMax(0, i - depth);

      if(!LeftOnly && i - depth < 0)
         continue;

      bool is_high = true;
      bool is_low = true;

      for(int j = newer_end; j <= older_end; j++)
      {
         if(high[j] > high[i])
            is_high = false;
         if(low[j] < low[i])
            is_low = false;
         if(!is_high && !is_low)
            break;
      }

      if(is_high)
      {
         if(pivot_count == 0 || MathAbs(high[i] - PivotValue[pivot_count - 1]) >= min_move)
            AddOrReplacePivot(pivot_count, i, high[i], 1, backstep);
      }

      if(is_low)
      {
         if(pivot_count == 0 || MathAbs(low[i] - PivotValue[pivot_count - 1]) >= min_move)
            AddOrReplacePivot(pivot_count, i, low[i], -1, backstep);
      }
   }

   for(int p = 0; p < pivot_count; p++)
   {
      int idx = PivotIndex[p];
      ZigZagBuffer[idx] = PivotValue[p];
      if(PivotType[p] == 1)
         HighPivotBuffer[idx] = PivotValue[p];
      else
         LowPivotBuffer[idx] = PivotValue[p];
   }

   for(int k = 1; k < pivot_count; k++)
   {
      int a = PivotIndex[k - 1];
      int b = PivotIndex[k];
      double av = PivotValue[k - 1];
      double bv = PivotValue[k];
      int span = a - b;
      if(span <= 0)
         continue;

      for(int x = a; x >= b; x--)
      {
         double t = (double)(a - x) / (double)span;
         ZigZagBuffer[x] = av + (bv - av) * t;
      }
   }

   return rates_total;
}
//+------------------------------------------------------------------+
