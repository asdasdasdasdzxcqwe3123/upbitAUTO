import pyupbit
import pytz
import time
import json
from datetime import datetime
import os
import requests
import signal
import pandas as pd

class UpbitMomentumStrategy:
    def __init__(self, config_path='config.json'):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)

            self.upbit = pyupbit.Upbit(config['upbit']['access_key'], config['upbit']['secret_key'])
            self.telegram_bot_token = config['telegram']['bot_token']
            self.telegram_chat_id = config['telegram']['channel_id']
            self.manual_holdings = config['trading']['manual_holdings']
            self.exclude_coins = config['trading']['exclude_coins'] + self.manual_holdings
            self.max_slots = config['trading'].get('max_slots', 3)
            self.rebalancing_interval = config['trading'].get('rebalancing_interval', 10080) * 60 # 일 단위로 변환
            self.last_purchase_time = None
            self.holdings_file = 'holdings_data.json'

            self.load_holdings_data()
            self.send_telegram_message("🤖 자동매매 봇이 시작되었습니다.")
            self.sync_holdings_with_current_state()
            self.setup_signal_handlers()
        except Exception as e:
            raise Exception(f"초기화 중 오류 발생: {e}")

    def send_telegram_message(self, message):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage",
                json={"chat_id": self.telegram_chat_id, "text": message, "parse_mode": "HTML"}
            )
            if not response.ok:
                print(f"텔레그램 메시지 전송 실패: {response.text}")
        except Exception as e:
            print(f"텔레그램 메시지 전송 중 오류 발생: {e}")

    def setup_signal_handlers(self):
        def handler(signum, frame):
            self.send_telegram_message(f"⚠️ 프로그램이 {signal.Signals(signum).name}에 의해 종료되었습니다.")
            exit(0)
        for sig in [signal.SIGINT, signal.SIGTERM]:
            signal.signal(sig, handler)

    def get_btc_ma120(self):
        df = pyupbit.get_ohlcv("KRW-BTC", interval="day", count=120)
        return pyupbit.get_current_price("KRW-BTC") > df['close'].mean()

    def get_top20_market_cap(self):
        try:
            tickers = [ticker for ticker in pyupbit.get_tickers(fiat="KRW")
                       if ticker.split('-')[1] not in self.exclude_coins]
            response = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": 300, "page": 1, "sparkline": False}
            )
            response.raise_for_status()
            top_coins = {coin['symbol'].upper(): coin for coin in response.json()}
            market_caps = [
                (f"KRW-{symbol}", coin['market_cap'], coin['market_cap_rank'])
                for ticker in tickers
                if (symbol := ticker.split('-')[1].upper()) in top_coins and (coin := top_coins[symbol]).get('market_cap')
            ]
            top20 = sorted(market_caps, key=lambda x: x[1], reverse=True)[:20]
            market_cap_msg = "📊 시가총액 상위 20개 코인:\n" + "\n".join(
                [f"{i+1}. {ticker} (세계 순위: #{rank}) - ${cap/1e9:.1f}B"
                 for i, (ticker, cap, rank) in enumerate(top20)]
            )
            self.send_telegram_message(market_cap_msg)
            return [item[0] for item in top20]
        except Exception as e:
            self.send_telegram_message(f"❌ 시가총액 상위 코인 조회 중 오류 발생: {e}")
            time.sleep(1)
            return []

    def check_trade_threshold(self):
        sold = []
        try:
            for balance in self.upbit.get_balances():
                currency = balance['currency']
                # 원화/수동 보유 코인은 스킵
                if currency in self.manual_holdings or currency == 'KRW':
                    continue

                balance_amt = float(balance['balance'])
                avg_price = float(balance['avg_buy_price'])
                if balance_amt * avg_price < 10000:
                    continue

                ticker = f"KRW-{currency}"
                current_price = pyupbit.get_current_price(ticker)
                if not current_price:
                    self.send_telegram_message(f"⚠️ {ticker} 현재가 조회 실패")
                    continue

                # trade_conditions에서 손절/익절 값 로드
                trade_condition = self.trade_conditions.get(ticker, {})
                stop_loss = trade_condition.get("stop_loss")
                take_profit = trade_condition.get("take_profit")

                if stop_loss is None or take_profit is None:
                    continue  # 손절/익절 값이 없으면 매도 판단 안 함

                # 손절 또는 익절 조건 체크
                if current_price <= stop_loss or current_price >= take_profit:
                    reason = "손절" if current_price <= stop_loss else "익절"
                    msg = (f"⚠️ {ticker} {reason} 실행\n"
                        f"현재가: {current_price:,.0f}, 손절가: {stop_loss:,.0f}, 익절가: {take_profit:,.0f}")
                    self.send_telegram_message(msg)

                    try:
                        self.upbit.sell_market_order(ticker, balance_amt)
                        self.send_telegram_message(f"✅ {ticker} 매도 완료 ({reason})")
                        sold.append(ticker)
                    except Exception as e:
                        self.send_telegram_message(f"❌ {ticker} 매도 실패: {e}")

            # 매도 완료 후 보유 정보 동기화
            self.sync_holdings_with_current_state()

        except Exception as e:
            self.send_telegram_message(f"❌ 거래 임계값 체크 중 오류 발생: {e}")
        return sold


    def calculate_7day_returns(self, tickers):
        returns = {}
        for ticker in tickers:
            df = pyupbit.get_ohlcv(ticker, interval="day", count=8)
            if df is not None and len(df) >= 7:
                returns[ticker] = ((df['close'].iloc[-1] - df['close'].iloc[-7]) / df['close'].iloc[-7]) * 100
            time.sleep(0.2)
        #self.send_telegram_message(f"📈 7일 수익률: {returns}")
        sorted_returns = sorted(returns.items(), key=lambda x: x[1], reverse=True)
        top3 = sorted_returns[:3]
        self.send_telegram_message(f"🔝 7일 수익률 상위 3개: {top3}")
        return [coin[0] for coin in top3]

    def get_top_momentum(self, top_n=20):
        """
        7일 수익률 기준 상위 N개 코인 반환
        :param top_n: 상위 코인 개수
        :return: 상위 N개 코인의 티커 리스트
        """
        tickers = [ticker for ticker in pyupbit.get_tickers(fiat="KRW") if ticker.split('-')[1] not in self.exclude_coins]
        returns = {}
        for ticker in tickers:
            df = pyupbit.get_ohlcv(ticker, interval="day", count=8)
            if df is not None and len(df) >= 7:
                returns[ticker] = ((df['close'].iloc[-1] - df['close'].iloc[-7]) / df['close'].iloc[-7]) * 100
            time.sleep(0.2)  # API 호출 제한 방지

        sorted_returns = sorted(returns.items(), key=lambda x: x[1], reverse=True)
        top_momentum = sorted_returns[:top_n]
        #주석처리
        #self.send_telegram_message(f"📈 7일 수익률 상위 {top_n}개 코인: {top_momentum}")
        return [coin[0] for coin in top_momentum]

    def should_keep_coin(self, ticker):
        now = datetime.now()
        holding_days = (now - self.holding_periods.get(ticker, now)).days
        if holding_days >= 14 or self.consecutive_holds.get(ticker, 0) >= 3:
            return False
        return True

    def load_holdings_data(self):
        """
        holdings_data.json 파일을 로드하여
        - self.holding_periods: 티커 -> datetime
        - self.consecutive_holds: 티커 -> int
        - self.trade_conditions: 티커 -> {'stop_loss': float, 'take_profit': float}
        형태로 불러오도록 수정
        """
        try:
            if os.path.exists(self.holdings_file):
                with open(self.holdings_file, 'r') as f:
                    data = json.load(f)

                # holding_periods는 "KRW-BTC" 같은 티커에 대해 datetime을 저장
                raw_periods = data.get('holding_periods', {})
                self.holding_periods = {}
                for ticker, val in raw_periods.items():
                    # val이 isoformat 문자열이면 datetime으로 변환
                    # 아니면(잘못된 값이면) 무시하거나 예외처리
                    try:
                        self.holding_periods[ticker] = datetime.fromisoformat(val)
                    except ValueError:
                        # 혹은 원하는 방법으로 처리
                        print(f"[load_holdings_data] {ticker} : 잘못된 날짜 형식 -> 무시")
                
                # 연속 보유 횟수
                self.consecutive_holds = data.get('consecutive_holds', {})

                # trade_conditions에 손절/익절 저장
                # 예: { 'KRW-BTC': {'stop_loss': 12000.0, 'take_profit': 16000.0}, ... }
                self.trade_conditions = data.get('trade_conditions', {})

                self.send_telegram_message("✅ 보유 데이터 로드 완료.")
            else:
                # 파일이 없으면 딕셔너리 초기화
                self.holding_periods = {}
                self.consecutive_holds = {}
                self.trade_conditions = {}
                self.send_telegram_message("⚠️ holdings_data.json이 존재하지 않아 초기화합니다.")

            # 가장 오래된 보유 기간을 기준으로 last_purchase_time 설정
            if self.holding_periods:
                self.last_purchase_time = min(self.holding_periods.values())
            else:
                self.last_purchase_time = None

        except Exception as e:
            self.send_telegram_message(f"❌ 보유 정보 로드 중 오류 발생: {e}")
            self.holding_periods = {}
            self.consecutive_holds = {}
            self.trade_conditions = {}
            self.last_purchase_time = None


    def save_holdings_data(self):
        """
        holding_periods는 { 'KRW-BTC': datetime }, trade_conditions는 { 'KRW-BTC': {'stop_loss': float, 'take_profit': float} }
        형식으로 저장
        """
        try:
            # 1) holding_periods에서 datetime -> isoformat 변환
            periods_data = {}
            for k, v in self.holding_periods.items():
                if isinstance(v, datetime):
                    periods_data[k] = v.isoformat()
                else:
                    # 혹시 모를 예외 처리
                    periods_data[k] = str(v)

            # 2) 딕셔너리 통째로 JSON에 넣을 구조
            data = {
                'holding_periods': periods_data,
                'consecutive_holds': self.consecutive_holds,
                'trade_conditions': self.trade_conditions
            }

            with open(self.holdings_file, 'w') as f:
                json.dump(data, f, indent=4)

            # 성공 시 로깅
            # self.send_telegram_message("✅ 보유 정보 저장 완료")  # 필요 시 활성화
        except Exception as e:
            self.send_telegram_message(f"❌ 보유 정보 저장 중 오류 발생: {e}")

    def sync_holdings_with_current_state(self):
        """
        현재 잔고와 저장된 holding_periods, trade_conditions를 동기화.
        - 더 이상 보유하지 않는 코인은 holding_periods, consecutive_holds, trade_conditions에서 제거
        - 새로 보유하게 된 코인은 holding_periods에 기록, trade_conditions는 아직 손절/익절 미지정이면 기본값 설정
        """
        try:
            # 현재 보유 중인 티커( KRW-XXX 형태 )
            current_holdings = {
                f"KRW-{balance['currency']}"
                for balance in self.upbit.get_balances()
                if (
                    float(balance['balance']) > 0 and
                    balance['currency'] not in self.manual_holdings and
                    float(balance['balance']) * float(balance['avg_buy_price']) >= 10000
                )
            }

            # 기존에 기록되어 있던 티커(예: holding_periods, trade_conditions에 있는 모든 티커)
            recorded_tickers = set(self.holding_periods.keys())

            # 보유하지 않는 코인은 제거
            for ticker in recorded_tickers - current_holdings:
                self.holding_periods.pop(ticker, None)
                self.consecutive_holds.pop(ticker, None)
                self.trade_conditions.pop(ticker, None)  # 손절/익절 조건 제거

            # 새로 보유하게 된 코인 추가
            for ticker in current_holdings - recorded_tickers:
                self.holding_periods[ticker] = datetime.now()
                self.consecutive_holds[ticker] = self.consecutive_holds.get(ticker, 0) + 1

                # trade_conditions에 아직 등록 안 된 경우 기본값 세팅
                if ticker not in self.trade_conditions:
                    self.trade_conditions[ticker] = {
                        "stop_loss": None,
                        "take_profit": None
                    }

            self.save_holdings_data()

        except Exception as e:
            self.send_telegram_message(f"❌ 보유 상태 동기화 중 오류 발생: {e}")
    
    def calculate_atr(self, df, window=14):
        """
        ATR(평균 진폭)을 계산
        :param df: Pandas DataFrame, OHLCV 데이터
        :param window: ATR 계산 기간 (기본값은 14일)
        :return: ATR 값
        """
        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift())
        low_close = abs(df['low'] - df['close'].shift())
        true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = true_range.rolling(window=window).mean()
        return atr.iloc[-1]

    def calculate_dynamic_k(self, df, base_k=0.5):
        """
        동적 k 값 계산
        :param df: OHLCV 데이터 (DataFrame)
        :param base_k: 기본 k 값
        :return: 동적으로 계산된 k 값
        """
        recent_volatility = (df['high'] - df['low']).mean()  # 최근 변동성 평균
        average_volatility = (df['high'] - df['low']).rolling(window=14).mean().iloc[-1]  # 14일 이동 평균 변동성

        # 변동성 비율을 기반으로 k 조정
        volatility_ratio = recent_volatility / average_volatility if average_volatility > 0 else 1
        dynamic_k = base_k * volatility_ratio

        # k 값이 지나치게 크거나 작아지지 않도록 제한
        return max(0.3, min(dynamic_k, 0.7))

    def calculate_breakout_price(self, df):
        yesterday = df.iloc[-2]
        today_open = df['open'].iloc[-1]
        volatility = yesterday['high'] - yesterday['low']

        # 동적 k 값 계산
        dynamic_k = self.calculate_dynamic_k(df)

        target_price = today_open + (volatility * dynamic_k)
        return target_price


    def should_buy(self, df):
        breakout_price = self.calculate_breakout_price(df)  # 동적 k 값을 반영
        current_price = df['close'].iloc[-1]
        return current_price > breakout_price


    def execute_trades(self):
        try:
            # (1) 먼저 매도 로직
            current_holdings = [
                balance['currency']
                for balance in self.upbit.get_balances()
                if (float(balance['balance']) > 0 and
                    balance['currency'] not in self.manual_holdings and
                    float(balance['balance']) * float(balance['avg_buy_price']) >= 10000)
            ]
            sold = []

            # 보유 코인 중에서 "should_keep_coin"이 False인 것(매도 대상)만 전량 매도
            for coin in current_holdings:
                ticker = f"KRW-{coin}"
                if not self.should_keep_coin(ticker):
                    try:
                        balance_amt = self.upbit.get_balance(coin)
                        self.send_telegram_message(f"🔄 {ticker} 전량 매도 시도 중...")
                        self.upbit.sell_market_order(ticker, balance_amt)
                        self.send_telegram_message(f"✅ {ticker} 매도 완료")
                        sold.append(ticker)

                        # 보유 정보 제거
                        self.holding_periods.pop(ticker, None)
                        self.consecutive_holds[ticker] = 0
                        self.trade_conditions.pop(ticker, None)

                    except Exception as e:
                        self.send_telegram_message(f"❌ {ticker} 매도 실패: {e}")

            # (2) 매수 로직
            # 매수하기 전에 최신 KRW 잔고와 보유 슬롯 계산
            total_krw_balance = float(self.upbit.get_balance("KRW"))
            # 이미 보유 중인 (자동매매 대상) 코인 수
            holding_count = len([
                c for c in current_holdings
                if float(self.upbit.get_balance(c)) * float(self.upbit.get_avg_buy_price(c)) >= 10000
            ])
            # 현재 매수 가능한 슬롯(최대 보유 코인 개수 - 현재 보유 코인 수)
            available_slots = self.max_slots - holding_count

            # 슬롯이 없거나 잔고가 적으면 매수 로직 건너뜀
            if available_slots <= 0 or total_krw_balance < 5000:
                return

            # 모멘텀 상위 코인 선정 (예: 20개)
            target_coins = self.get_top_momentum(top_n=20)

            for ticker in target_coins:
                # 슬롯을 모두 소진했으면 중단
                if available_slots <= 0:
                    break

                # 이미 매도된(sold) 코인, 혹은 현재 보유중인 코인이면 스킵
                if ticker in sold or ticker in [f"KRW-{c}" for c in current_holdings]:
                    continue

                # 변동성 돌파 여부 확인
                df = pyupbit.get_ohlcv(ticker, interval="minute60", count=48)
                if df is None or not self.should_buy(df):
                    continue

                # 각 코인 매수 시점마다 잔고를 재확인
                krw_balance = float(self.upbit.get_balance("KRW"))
                # 잔고가 부족하면 더 이상 매수 불가 -> 중단
                if krw_balance < 5000:
                    break

                # invest: 남은 잔고를 현재 available_slots에 맞게 분할
                invest = max(int(krw_balance / available_slots / 1000) * 990, 5000)
                if invest > krw_balance:
                    break  # 투자액이 실제 잔고보다 많으면 매수 불가 -> 중단

                # 손절/익절 기준 계산
                breakout_price = self.calculate_breakout_price(df)
                atr = self.calculate_atr(df)
                stop_loss = breakout_price - (1.5 * atr)
                take_profit = breakout_price + (1.5 * atr)

                # 매수 시도
                try:
                    self.send_telegram_message(
                        f"🛒 {ticker} 매수 시도 (투자액: {invest:,}원 / 잔고: {krw_balance:,.0f}원 / 슬롯: {available_slots})"
                    )
                    self.upbit.buy_market_order(ticker, invest)
                    self.send_telegram_message(
                        f"✅ {ticker} 매수 완료 | 목표가: {breakout_price:.0f}, 손절가: {stop_loss:.0f}, 익절가: {take_profit:.0f}"
                    )

                    # 손절/익절 조건 저장
                    self.trade_conditions[ticker] = {
                        "stop_loss": stop_loss,
                        "take_profit": take_profit
                    }
                    # 보유 기간/연속 보유 횟수 갱신
                    self.holding_periods[ticker] = datetime.now()
                    self.consecutive_holds[ticker] = self.consecutive_holds.get(ticker, 0) + 1

                    # 현재 보유목록 갱신 + 슬롯 1개 소모
                    current_holdings.append(ticker.split('-')[1])
                    available_slots -= 1

                except Exception as e:
                    self.send_telegram_message(f"❌ {ticker} 매수 실패: {e}")
                    # 매수 실패 시 슬롯 차감 여부는 전략에 맞게 결정 (여기서는 차감 안 함)

            # 매수/매도 끝난 뒤 최종 저장
            self.save_holdings_data()

        except Exception as e:
            self.send_telegram_message(f"❌ 매매 실행 중 오류 발생: {e}")



    def sell_all_positions(self):
        try:
            for balance in self.upbit.get_balances():
                currency = balance['currency']

                if currency in self.manual_holdings or float(balance['balance']) * float(balance['avg_buy_price']) < 10000:
                    continue

                ticker = f"KRW-{currency}"

                try:
                    balance_amt = self.upbit.get_balance(currency)
                    self.send_telegram_message(f"🔄 {ticker} 전량 매도 시도 중...")
                    self.upbit.sell_market_order(ticker, balance_amt)
                    self.send_telegram_message(f"✅ {ticker} 매도 완료")
                    self.holding_periods.pop(ticker, None)
                    self.consecutive_holds[ticker] = 0

                except Exception as e:
                    self.send_telegram_message(f"❌ {ticker} 매도 실패: {e}")

        except Exception as e:
            self.send_telegram_message(f"❌ 전체 매도 중 오류 발생: {e}")

    def run(self):
        is_suspended = False
        kst = pytz.timezone('Asia/Seoul')
        while True:
            try:
                now = datetime.now(kst)
                btc_above_ma = self.get_btc_ma120()  # BTC 120일 이평선 상위인지 확인
                sold_coins = self.check_trade_threshold()  # 손절 및 수익 실현 체크 후 매도
                self.sync_holdings_with_current_state()

                if not btc_above_ma:
                    if not is_suspended:
                        self.send_telegram_message("😱 BTC가 120일 이평선 아래로 떨어져 전체 매도 후 매매를 중지합니다.")
                        self.sell_all_positions()
                        is_suspended = True
                else:
                    if is_suspended:  # 매매 재개 체크
                        self.send_telegram_message("✅ BTC가 120일 이평선 위 올라왔습니다. 매매를 재개합니다.")
                        is_suspended = False

                    # 보유 코인 개수 확인 (1만 원 이하 자산 제외)
                    holding_count = len([
                        balance['currency']
                        for balance in self.upbit.get_balances()
                        if (
                            float(balance['balance']) > 0 and
                            balance['currency'] not in self.manual_holdings and
                            float(balance['balance']) * float(balance['avg_buy_price']) >= 10000  # 1만 원 이상인 자산만 포함
                        )
                    ])

                    # 손절 매도가 없고 보유 코인 수가 max_slots보다 작은 경우
                    if (not sold_coins) and (holding_count < self.max_slots) and (not is_suspended):
                        self.send_telegram_message(f"보유 코인이 {self.max_slots}개 보다 적은 상태입니다. 매매를 실행합니다.")
                        self.execute_trades()
                    # 리밸런싱 주기마다 매매 실행
                    elif (self.last_purchase_time is not None) and (
                            now.weekday() == 0 and now.hour == 23 and 29 <= now.minute < 31):
                        self.send_telegram_message(f"리밸런싱 주기가 도래하여 매매를 실행합니다.")
                        self.execute_trades()

                time.sleep(60)
            except Exception as e:
                self.send_telegram_message(f"❌ 실행 중 오류 발생: {e}")
                time.sleep(60)



if __name__ == "__main__":
    try:
        UpbitMomentumStrategy().run()
    except Exception as e:
        print(f"오류 발생: {e}")
