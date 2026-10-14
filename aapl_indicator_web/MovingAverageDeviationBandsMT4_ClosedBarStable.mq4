//+------------------------------------------------------------------+
//| MovingAverageDeviationBandsMT4_ClosedBarStable.mq4               |
//| Rebuilds the screenshot setup and avoids live-bar width drift.    |
//| Center: EMA13 on HLC3. Width: max(StdDev13, Wilder ATR14) * 0.5. |
//+------------------------------------------------------------------+
#property strict
#property indicator_chart_window
#property indicator_buffers 3
#property indicator_plots   3

#property indicator_label1  "Upper 1 sigma"
#property indicator_type1   DRAW_LINE
#property indicator_color1  clrDeepSkyBlue
#property indicator_style1  STYLE_SOLID
#property indicator_width1  1

#property indicator_label2  "Middle"
#property indicator_type2   DRAW_LINE
#property indicator_color2  clrMediumSlateBlue
#property indicator_style2  STYLE_SOLID
#property indicator_width2  1

#property indicator_label3  "Lower 1 sigma"
#property indicator_type3   DRAW_LINE
#property indicator_color3  clrDeepSkyBlue
#property indicator_style3  STYLE_SOLID
#property indicator_width3  1

input int    Period                    = 13;    // Screenshot: 13
input int    Shift                     = 0;     // Screenshot: 0
input double DeviationMultiplierGlobal = 0.5;   // Screenshot: 0.5
input int    AtrPeriod                 = 14;    // Screenshot: 14
input double Sigma1                    = 1.0;   // Screenshot: 1.0
input bool   UseClosedBarWidth         = true;  // Fix: ignore live bar width
input bool   ShowMiddle                = true;

double UpperBuffer[];
double MiddleBuffer[];
double LowerBuffer[];
double WidthBuffer[];
double AtrBuffer[];

double Hlc3At(const int i, const double &high[], const double &low[], const double &close[])
{
   return (high[i] + low[i] + close[i]) / 3.0;
}

double TrueRangeAt(
   const int i,
   const int rates_total,
   const double &high[],
   const double &low[],
   const double &close[]
)
{
   if(i >= rates_total - 1)
      return high[i] - low[i];

   double previous_close = close[i + 1];
   return MathMax(
      high[i] - low[i],
      MathMax(MathAbs(high[i] - previous_close), MathAbs(low[i] - previous_close))
   );
}

double StdDevAt(
   const int i,
   const int rates_total,
   const int period,
   const double &high[],
   const double &low[],
   const double &close[]
)
{
   int count = MathMin(period, rates_total - i);
   if(count <= 1)
      return 0.0;

   double sum = 0.0;
   for(int k = 0; k < count; k++)
      sum += Hlc3At(i + k, high, low, close);

   double mean = sum / count;
   double variance = 0.0;
   for(int j = 0; j < count; j++)
   {
      double diff = Hlc3At(i + j, high, low, close) - mean;
      variance += diff * diff;
   }

   return MathSqrt(variance / count);
}

int OnInit()
{
   IndicatorShortName("MA Deviation Bands ClosedBar Stable");

   IndicatorBuffers(5);
   SetIndexBuffer(0, UpperBuffer, INDICATOR_DATA);
   SetIndexBuffer(1, MiddleBuffer, INDICATOR_DATA);
   SetIndexBuffer(2, LowerBuffer, INDICATOR_DATA);
   SetIndexBuffer(3, WidthBuffer, INDICATOR_CALCULATIONS);
   SetIndexBuffer(4, AtrBuffer, INDICATOR_CALCULATIONS);

   SetIndexLabel(0, "Upper 1 sigma");
   SetIndexLabel(1, "Middle");
   SetIndexLabel(2, "Lower 1 sigma");

   SetIndexStyle(0, DRAW_LINE, STYLE_SOLID, 1, clrDeepSkyBlue);
   SetIndexStyle(1, ShowMiddle ? DRAW_LINE : DRAW_NONE, STYLE_SOLID, 1, clrMediumSlateBlue);
   SetIndexStyle(2, DRAW_LINE, STYLE_SOLID, 1, clrDeepSkyBlue);

   SetIndexShift(0, Shift);
   SetIndexShift(1, Shift);
   SetIndexShift(2, Shift);

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

   int period = MathMax(1, Period);
   int atr_period = MathMax(1, AtrPeriod);
   double ema_alpha = 2.0 / (period + 1.0);
   double atr_alpha = 1.0 / atr_period;
   double multiplier = MathMax(0.0, DeviationMultiplierGlobal) * MathMax(0.0, Sigma1);

   // Full recalculation is deliberate. The width depends on ATR and StdDev
   // state, so short prev_calculated updates can make the live template state
   // differ from a freshly loaded template.
   double ema = 0.0;
   double atr = 0.0;

   for(int i = rates_total - 1; i >= 0; i--)
   {
      double source = Hlc3At(i, high, low, close);
      double tr = TrueRangeAt(i, rates_total, high, low, close);

      if(i == rates_total - 1)
      {
         ema = source;
         atr = tr;
      }
      else
      {
         ema = ema_alpha * source + (1.0 - ema_alpha) * ema;
         atr = atr_alpha * tr + (1.0 - atr_alpha) * atr;
      }

      double stddev = StdDevAt(i, rates_total, period, high, low, close);
      AtrBuffer[i] = atr;
      WidthBuffer[i] = MathMax(stddev, atr);
      MiddleBuffer[i] = ema;
   }

   for(int j = rates_total - 1; j >= 0; j--)
   {
      int width_index = j;
      if(UseClosedBarWidth && j == 0 && rates_total > 1)
         width_index = 1;

      double width = WidthBuffer[width_index] * multiplier;
      UpperBuffer[j] = MiddleBuffer[j] + width;
      LowerBuffer[j] = MiddleBuffer[j] - width;
   }

   return rates_total;
}
//+------------------------------------------------------------------+
