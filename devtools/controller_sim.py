"""Эмулятор контроллеров для разработки и демонстрации.

Ведёт себя как реальное железо: каждый датчик публикует float-строку в свой
MQTT-топик, контроллер никак себя не обозначает. Умеет играть сценарии:
норма, открытая дверца (короткие забросы), перегрев, отказ датчика, молчание.

Запуск (из backend-виртуального окружения):
    python devtools/controller_sim.py --host localhost --controllers 2
    python devtools/controller_sim.py --scenario overheat:1/2   # контроллер 1, датчик 2
    python devtools/controller_sim.py --scenario offline:2/1 --scenario door:1
"""

import argparse
import asyncio
import math
import random
import time

import aiomqtt


class SensorSim:
    def __init__(self, controller_idx: int, sensor_idx: int, args: argparse.Namespace) -> None:
        self.topic = f"cw-sim/{controller_idx:02d}{sensor_idx}ab{random.randint(100, 999)}/temp"
        self.controller_idx = controller_idx
        self.sensor_idx = sensor_idx
        self.base = args.base + random.uniform(-0.6, 0.6)
        self.phase = random.uniform(0, math.tau)
        self.door_open_until = 0.0
        key = f"{controller_idx}/{sensor_idx}"
        self.overheat = key in args.overheat or str(controller_idx) in args.overheat
        self.offline = key in args.offline or str(controller_idx) in args.offline
        self.fault = key in args.fault
        self.door = str(controller_idx) in args.door

    def value(self, now: float) -> float | None:
        if self.offline:
            return None
        if self.fault:
            return 85.0  # заклинивший датчик — неправдоподобное значение
        # медленное «дыхание» компрессора + шум
        temp = self.base + 1.2 * math.sin(now / 300 + self.phase) + random.gauss(0, 0.08)
        if self.door and now < self.door_open_until:
            temp += 6.0 * random.uniform(0.7, 1.0)
        elif self.door and random.random() < 0.002:
            self.door_open_until = now + random.uniform(20, 90)
        if self.overheat:
            temp += 9.0
        return round(temp, 2)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--controllers", type=int, default=2)
    parser.add_argument("--interval", type=float, default=5.0, help="период публикации, сек")
    parser.add_argument("--base", type=float, default=4.5, help="базовая температура, °C")
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="overheat:C[/S] | offline:C[/S] | fault:C/S | door:C (можно повторять)",
    )
    args = parser.parse_args()

    args.overheat, args.offline, args.fault, args.door = set(), set(), set(), set()
    for scenario in args.scenario:
        kind, _, target = scenario.partition(":")
        getattr(args, kind).add(target)

    sensors = [
        SensorSim(controller, sensor, args)
        for controller in range(1, args.controllers + 1)
        for sensor in range(1, 4)
    ]
    print(f"Simulating {args.controllers} controllers ({len(sensors)} sensors):")
    for sensor in sensors:
        print(f"  {sensor.topic}")

    async with aiomqtt.Client(hostname=args.host, port=args.port) as client:
        while True:
            now = time.monotonic()
            for sensor in sensors:
                value = sensor.value(now)
                if value is not None:
                    await client.publish(sensor.topic, str(value))
            await asyncio.sleep(args.interval)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
