from asyncio import create_task, wait_for
from datetime import datetime, timezone
from io import StringIO
from typing import AbstractSet, Optional, Text

from httpx import HTTPStatusError
from nonebot import on_command
from nonebot.internal.adapter import Event
from nonebot_plugin_saa import MessageFactory
from ssttkkl_nonebot_utils.errors.errors import BadRequestError, QueryError
from ssttkkl_nonebot_utils.interceptor.handle_error import handle_error
from ssttkkl_nonebot_utils.interceptor.with_handling_reaction import with_handling_reaction
from ssttkkl_nonebot_utils.nonebot import default_command_start

from .data.api import paifuya_api as api
from .data.models.player_num import PlayerNum
from .data.models.room_rank import all_four_player_room_rank, all_three_player_room_rank, RoomRank
from .mappers.player_extended_stats import map_player_extended_stats
from .mappers.player_stats import map_player_stats
from .mappers.room_rank import map_room_rank
from .parsers.limit_of_games import try_parse_limit_of_games
from .parsers.name import try_parse_name, get_name_in_unconsumed_args
from .parsers.room_rank import try_parse_room_rank
from .parsers.time_span import try_parse_time_span
from ..ac import query_info_service
from ..config import conf
from ..errors import error_handlers


def make_handler(player_num: PlayerNum):
    async def majsoul_info(event: Event):
        args = event.get_message().extract_plain_text().split()
        cmd, args = args[0], args[1:]

        unconsumed_args = []
        kwargs = {}

        for arg in args:
            if "room_rank" not in kwargs:
                room_rank = try_parse_room_rank(arg)
                if room_rank is not None:
                    if player_num == PlayerNum.four:
                        kwargs["room_rank"] = room_rank[0]
                    elif player_num == PlayerNum.three:
                        kwargs["room_rank"] = room_rank[1]
                    continue

            if "time_span" not in kwargs:
                time_span = try_parse_time_span(arg)
                if time_span is not None:
                    kwargs["start_time"], kwargs["end_time"] = time_span
                    continue

            if "limit" not in kwargs:
                limit = try_parse_limit_of_games(arg)
                if limit is not None:
                    kwargs["limit"] = limit
                    continue

            if "nickname" not in kwargs:
                name = try_parse_name(arg)
                if name is not None:
                    kwargs["nickname"] = name
                    continue

            unconsumed_args.append(arg)

        if "nickname" not in kwargs:
            nickname = await get_name_in_unconsumed_args(unconsumed_args)

            if not nickname:
                raise BadRequestError("请输入雀魂账号")

            kwargs["nickname"] = nickname

        coro = handle_majsoul_info(player_num=player_num, **kwargs)
        if conf.majsoul_query_timeout:
            await wait_for(coro, timeout=conf.majsoul_query_timeout)
        else:
            await coro

    return majsoul_info


four_player_majsoul_info_matcher = on_command('雀魂信息', aliases={'雀魂查询'})
query_info_service.patch_matcher(four_player_majsoul_info_matcher)
four_player_majsoul_info_matcher.__help_info__ = (f"{default_command_start}雀魂信息 <雀魂账号> "
                                                  f"[<房间类型>] [最近<数量>场] [最近<数量>{{天|周|个月|年}}]")
four_player_majsoul_info = make_handler(PlayerNum.four)
four_player_majsoul_info = with_handling_reaction()(four_player_majsoul_info)
four_player_majsoul_info = handle_error(error_handlers)(four_player_majsoul_info)
four_player_majsoul_info_matcher.append_handler(four_player_majsoul_info)

three_player_majsoul_info_matcher = on_command('雀魂三麻信息', aliases={'雀魂三麻查询'})
query_info_service.patch_matcher(three_player_majsoul_info_matcher)
three_player_majsoul_info_matcher.__help_info__ = (f"{default_command_start}雀魂三麻信息 <雀魂账号> "
                                                   f"[<房间类型>] [最近<数量>场] [最近<数量>{{天|周|个月|年}}]")
three_player_majsoul_info = make_handler(PlayerNum.three)
three_player_majsoul_info = with_handling_reaction()(three_player_majsoul_info)
three_player_majsoul_info = handle_error(error_handlers)(three_player_majsoul_info)
three_player_majsoul_info_matcher.append_handler(three_player_majsoul_info)


async def handle_majsoul_info(
    nickname: str,
    player_num: PlayerNum,
    *,
    room_rank: Optional[AbstractSet[RoomRank]] = None,
    start_time: Optional[datetime] = None,
    end_time: Optional[datetime] = None,
    limit: Optional[int] = None
):
    default_start_time = start_time is None
    default_end_time = end_time is None
    default_limit = limit is None

    if room_rank is None:
        if player_num == PlayerNum.four:
            room_rank = all_four_player_room_rank
        elif player_num == PlayerNum.three:
            room_rank = all_three_player_room_rank

    if start_time is None:
        start_time = datetime.fromisoformat("2010-01-01T00:00:00").astimezone(timezone.utc)

    if end_time is None:
        end_time = datetime.now(timezone.utc)

    sio = StringIO()

    #  查询玩家
    players = await api[player_num].search_player(nickname)

    # 优先完全匹配
    exact_players = [p for p in players if p.nickname == nickname]
    if exact_players:
        players = exact_players

    if not players:
        raise QueryError("没有查询到该角色在金之间以上的对局数据呢~")

    if len(players) > 1:
        sio.write("查询到多个同名账号，已分别统计：\n\n")

    room_rank_text = map_room_rank(room_rank)

    # 多账号逐个统计
    for idx, p in enumerate(players):
        sio.write(f"====== 昵称：{p.nickname} ======\n")

        try:
            if limit is not None:
                records = await api[player_num].player_records(
                    p.id,
                    start_time,
                    end_time,
                    room_rank,
                    limit=limit,
                    descending=True
                )
                if records:
                    start_time_used = records[-1].start_time
                else:
                    start_time_used = start_time
            else:
                start_time_used = start_time

            stats_task = create_task(api[player_num].player_stats(
                p.id,
                start_time_used,
                end_time,
                room_rank
            ))
            ext_task = create_task(api[player_num].player_extended_stats(
                p.id,
                start_time_used,
                end_time,
                room_rank
            ))

            player_stats = await stats_task
            player_extended_stats = await ext_task

        except HTTPStatusError as e:
            if e.response.status_code == 404:
                sio.write("没有该账号的对局数据\n\n")
                continue
            else:
                raise e

        if player_stats is None:
            sio.write(f"没有查询到{room_rank_text}的对局数据呢~\n\n")
            continue

        # 写入统计信息
        map_player_stats(sio, player_stats, room_rank_text, player_num)
        sio.write("\n")
        map_player_extended_stats(sio, player_extended_stats, room_rank_text)

        sio.write("\n\n")

        # 链接
        if conf.majsoul_send_link:
            sio.write("更多信息：")

            if player_num == PlayerNum.four:
                url = f"https://amae-koromo.sapk.ch/player/{p.id}/"
            else:
                url = f"https://ikeda.sapk.ch/player/{p.id}/"

            url += ".".join(map(lambda x: str(x.value), room_rank))

            if not default_start_time:
                url += "/" + start_time.strftime("%Y-%m-%d")
            if not default_end_time:
                url += "/" + end_time.strftime("%Y-%m-%d")
            if not default_limit:
                url += f"?limit={limit}"

            sio.write(url)
            sio.write("\n\n")

    # 输出
    msg = sio.getvalue()
    await MessageFactory(Text(msg.strip())).send(reply=True)
    
