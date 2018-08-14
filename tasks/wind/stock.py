# -*- coding: utf-8 -*-
"""
Created on 2017/4/14
@author: MG
"""
import math
import pandas as pd
import logging, os, json
from tasks.merge.code_mapping import update_from_info_table
from datetime import date, datetime, timedelta
from tasks.wind import invoker
from direstinvoker.ifind import APIError, UN_AVAILABLE_DATE
from tasks.utils.fh_utils import get_last, get_first, date_2_str, STR_FORMAT_DATE, str_2_date, unzip_join
from tasks import app
from sqlalchemy.types import String, Date, Integer
from sqlalchemy.dialects.mysql import DOUBLE
from tasks.backend import engine_md
from tasks.utils.db_utils import with_db_session, add_col_2_table
DEBUG = False
logger = logging.getLogger()
DATE_BASE = datetime.strptime('2005-01-01', STR_FORMAT_DATE).date()
ONE_DAY = timedelta(days=1)
# 标示每天几点以后下载当日行情数据
BASE_LINE_HOUR = 16


def get_stock_code_set(date_fetch):
    """
     # 通过接口获取股票代码
    :param date_fetch:
    :return:
    """
    date_fetch_str = date_fetch.strftime(STR_FORMAT_DATE)
    stock_df = invoker.wset("sectorconstituent", "date=%s;sectorid=a001010100000000" % date_fetch_str)
    if stock_df is None:
        logging.warning('%s 获取股票代码失败', date_fetch_str)
        return None
    stock_count = stock_df.shape[0]
    logging.info('get %d stocks on %s', stock_count, date_fetch_str)
    return set(stock_df['wind_code'])


@app.task
def import_wind_stock_info(refresh=False):
    # 获取全市场股票代码及名称
    table_name = 'wind_stock_info'
    logging.info("更新 wind_stock_info 开始")
    """获取date—fetch
    """
    if refresh:
        date_fetch = datetime.strptime('2005-1-1', STR_FORMAT_DATE).date()
    else:
        date_fetch = date.today()
    date_end = date.today()
    stock_code_set = set()
    # 对date_fetch 进行一个判断，获取stock_code_set
    while date_fetch < date_end:
        stock_code_set_sub = get_stock_code_set(date_fetch)
        if stock_code_set_sub is not None:
            stock_code_set |= stock_code_set_sub
        date_fetch += timedelta(days=365)
    stock_code_set_sub = get_stock_code_set(date_end)
    if stock_code_set_sub is not None:
        stock_code_set |= stock_code_set_sub
    # 获取股票对应上市日期，及摘牌日期
    # w.wss("300005.SZ,300372.SZ,000003.SZ", "ipo_date,trade_code,mkt,exch_city,exch_eng")
    stock_code_list = list(stock_code_set)
    stock_code_count = len(stock_code_list)
    seg_count = 1000
    loop_count = math.ceil(float(stock_code_count) / seg_count)
    stock_info_df_list = []
    # 进行循环遍历获取stock_code_list_sub
    for n in range(loop_count):
        num_start = n * seg_count
        num_end = (n + 1) * seg_count
        num_end = num_end if num_end <= stock_code_count else stock_code_count
        stock_code_list_sub = stock_code_list[num_start:num_end]
        # 尝试将 stock_code_list_sub 直接传递给wss，是否可行
        stock_info_df = invoker.wss(stock_code_list_sub,
                                    "sec_name,trade_code,ipo_date,delist_date,mkt,exch_city,exch_eng,prename")
        stock_info_df_list.append(stock_info_df)
    # 对数据表进行规范整理.整合,索引重命名
    stock_info_all_df = pd.concat(stock_info_df_list)
    stock_info_all_df.index.rename('WIND_CODE', inplace=True)
    logging.info('%s stock data will be import', stock_info_all_df.shape[0])
    stock_info_all_df.reset_index(inplace=True)
    data_list = list(stock_info_all_df.T.to_dict().values())
    # 对wind_stock_info表进行数据插入
    sql_str = "REPLACE INTO {table_name} (wind_code, trade_code, sec_name, ipo_date, delist_date, mkt, exch_city, exch_eng, prename) values (:WIND_CODE, :TRADE_CODE, :SEC_NAME, :IPO_DATE, :DELIST_DATE, :MKT, :EXCH_CITY, :EXCH_ENG, :PRENAME)"
    # sql_str = "insert INTO wind_stock_info (wind_code, trade_code, sec_name, ipo_date, delist_date, mkt, exch_city, exch_eng, prename) values (:WIND_CODE, :TRADE_CODE, :SEC_NAME, :IPO_DATE, :DELIST_DATE, :MKT, :EXCH_CITY, :EXCH_ENG, :PRENAME)"
    # 事物提交执行更新
    with with_db_session(engine_md) as session:
        session.execute(sql_str, data_list)
        stock_count = session.execute('select count(*) from {table_name}').scalar()
    logging.info("更新 %s 完成 存量数据 %d 条", {table_name}, stock_count)
    update_from_info_table(table_name)


@app.task
def import_stock_daily():
    """
    插入股票日线数据到最近一个工作日-1。
    如果超过 BASE_LINE_HOUR 时间，则获取当日的数据
    :return:
    """
    logging.info("更新 wind_stock_daily 开始")
    table_name = 'wind_stock_daily'
    param_list = [
        ('open', DOUBLE),
        ('high', DOUBLE),
        ('low', DOUBLE),
        ('close', DOUBLE),
        ('adjfactor', DOUBLE),
        ('volume', DOUBLE),
        ('amt', DOUBLE),
        ('pct_chg', DOUBLE),
        ('maxupordown', Integer),
        ('swing', DOUBLE),
        ('turn', DOUBLE),
        ('free_turn', DOUBLE),
        ('trade_status', String(20)),
        ('susp_days', Integer),
        ('total_shares', DOUBLE),
        ('free_float_shares', DOUBLE),
        ('ev2_to_ebitda', DOUBLE),
    ]
    wind_indictor_str = ",".join([key for key, _ in param_list])
    has_table = engine_md.has_table(table_name)
    # 进行表格判断，确定是否含有wind_stock_daily
    if has_table:
        sql_str = """
            SELECT wind_code, date_frm, if(delist_date<end_date, delist_date, end_date) date_to
            FROM
            (
            SELECT info.wind_code, ifnull(trade_date, ipo_date) date_frm, delist_date,
            if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
            FROM 
                wind_stock_info info 
            LEFT OUTER JOIN
                (SELECT wind_code, adddate(max(trade_date),1) trade_date FROM {table_name} GROUP BY wind_code) daily
            ON info.wind_code = daily.wind_code
            ) tt
            WHERE date_frm <= if(delist_date<end_date, delist_date, end_date) 
            ORDER BY wind_code""".format(table_name=table_name)
    else:
        logger.warning('wind_stock_daily 不存在，仅使用 wind_stock_info 表进行计算日期范围')
        sql_str = """
            SELECT wind_code, date_frm, if(delist_date<end_date, delist_date, end_date) date_to
            FROM
              (
                SELECT info.wind_code, ipo_date date_frm, delist_date,
                if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                FROM wind_stock_info info 
              ) tt
            WHERE date_frm <= if(delist_date<end_date, delist_date, end_date) 
            ORDER BY wind_code"""

    with with_db_session(engine_md) as session:
        # 获取每只股票需要获取日线数据的日期区间
        table = session.execute(sql_str)
        # 计算每只股票需要获取日线数据的日期区间
        begin_time = None
        # 获取date_from,date_to，将date_from,date_to做为value值
        code_date_range_dic = {
            wind_code: (date_from if begin_time is None else min([date_from, begin_time]), date_to)
            for wind_code, date_from, date_to in table.fetchall() if
            wind_code_set is None or wind_code in wind_code_set}
    # 设置 dtype
    dtype = {key: val for key, val in param_list}
    dtype['wind_code'] = String(20)
    dtype['trade_date'] = Date

    data_df_list = []
    data_len = len(code_date_range_dic)
    logger.info('%d stocks will been import into wind_stock_daily', data_len)
    # 将data_df数据，添加到data_df_list
    try:
        for num, (wind_code, (date_from, date_to)) in enumerate(code_date_range_dic.items(), start=1):
            logger.debug('%d/%d) %s [%s - %s]', num, data_len, wind_code, date_from, date_to)
            try:
                data_df = invoker.wsd(wind_code, wind_indictor_str, date_from, date_to)
            except APIError as exp:
                logger.exception("%d/%d) %s 执行异常", num, data_len, wind_code)
                if exp.ret_dic.setdefault('error_code', 0) in (
                        -40520007,  # 没有可用数据
                        -40521009,  # 数据解码失败。检查输入参数是否正确，如：日期参数注意大小月月末及短二月
                ):
                    continue
                else:
                    break
            if data_df is None:
                logger.warning('%d/%d) %s has no data during %s %s', num, data_len, wind_code, date_from, date_to)
                continue
            logger.info('%d/%d) %d data of %s between %s and %s', num, data_len, data_df.shape[0], wind_code, date_from,
                        date_to)
            data_df['wind_code'] = wind_code
            data_df_list.append(data_df)
            # 仅调试使用
            if DEBUG and len(data_df_list) > 4:
                break
    finally:
        # 导入数据库
        if len(data_df_list) > 0:
            data_df_all = pd.concat(data_df_list)
            data_df_all.index.rename('trade_date', inplace=True)
            data_df_all.reset_index(inplace=True)
            data_df_all.set_index(['wind_code', 'trade_date'], inplace=True)
            data_df_all.to_sql('wind_stock_daily', engine_md, if_exists='append', dtype=dtype)
            logging.info("更新 wind_stock_daily 结束 %d 条信息被更新", data_df_all.shape[0])


@app.task
def add_new_col_data(col_name, param, db_col_name=None, col_type_str='DOUBLE', wind_code_set: set = None):
    """
    1）修改 daily 表，增加字段
    2）wind_ckdvp_stock表增加数据
    3）第二部不见得1天能够完成，当第二部完成后，将wind_ckdvp_stock数据更新daily表中
    :param col_name:增加字段名称
    :param param: 参数
    :param db_col_name: 默认为 None，此时与col_name相同
    :param col_type_str: DOUBLE, VARCHAR(20), INTEGER, etc. 不区分大小写
    :param wind_code_set: 默认 None， 否则仅更新指定 wind_code
    :return:
    """
    if db_col_name is None:
        # 默认为 None，此时与col_name相同
        db_col_name = col_name

    # 检查当前数据库是否存在 db_col_name 列，如果不存在则添加该列
    add_col_2_table(engine_md, 'wind_stock_daily', db_col_name, col_type_str)
    # 将数据增量保存到 wind_ckdvp_stock 表
    all_finished = add_data_2_ckdvp(col_name, param, wind_code_set)
    # 将数据更新到 ds 表中
    # 对表的列进行整合，daily表的列属性值插入wind_ckdvp_stock的value 根据所有条件进行判定
    if all_finished:
        sql_str = """
            update wind_stock_daily daily, wind_ckdvp_stock ckdvp
            set daily.{db_col_name} = ckdvp.value
            where daily.wind_code = ckdvp.wind_code
            and ckdvp.key = '{db_col_name}' and ckdvp.param = '{param}'

            and ckdvp.time = daily.trade_date""".format(db_col_name=db_col_name, param=param)
        # 进行事务提交
        with with_db_session(engine_md) as session:
            rst = session.execute(sql_str)
            data_count = rst.rowcount
            session.commit()
        logger.info('更新 %s 字段 wind_stock_daily 表 %d 条记录', db_col_name, data_count)


def add_data_2_ckdvp(col_name, param, wind_code_set: set = None, begin_time=None):
    """判断表格是否存在，存在则进行表格的关联查询
    :param col_name: 增加的列属性名
    :param param: 参数
    :param wind_code_set: 默认为None
    :param begin_time: 默认为None
    :return:
    """
    table_name = 'wind_ckdvp_stock'
    all_finished = False
    has_table = engine_md.has_table('wind_ckdvp_stock')
    if has_table:
        # 执行语句，表格数据联立
        sql_str = """
            select wind_code, date_frm, if(delist_date<end_date, delist_date, end_date) date_to
            FROM
            (
                select info.wind_code,
                    (ipo_date) date_frm, delist_date,
                    if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                from wind_stock_info info
                left outer join
                    (select wind_code, adddate(max(time),1) from wind_ckdvp_stock
                    where wind_ckdvp_stock.key='{0}' and param='{1}' group by wind_code
                    ) daily
                on info.wind_code = daily.wind_code
            ) tt
            where date_frm <= if(delist_date<end_date,delist_date, end_date)
            order by wind_code""".format(col_name, param)
    else:
        logger.warning('wind_ckdvp_stock 不存在，仅使用 wind_stock_info 表进行计算日期范围')
        sql_str = """
            SELECT wind_code, date_frm,
                if(delist_date<end_date,delist_date, end_date) date_to
            FROM
            (
                SELECT info.wind_code,ipo_date date_frm, delist_date,
                if(hour(now())<16, subdate(curdate(),1), curdate()) end_date
                FROM wind_stock_info info
            ) tt
            WHERE date_frm <= if(delist_date<end_date, delist_date, end_date)
            ORDER BY wind_code"""
    with with_db_session(engine_md) as session:
        # 获取每只股票需要获取日线数据的日期区间
        table = session.execute(sql_str)
        code_date_range_dic = {
            wind_code: (date_from if begin_time is None else min([date_from, begin_time]), date_to)
            for wind_code, date_from, date_to in table.fetchall() if
            wind_code_set is None or wind_code in wind_code_set}

        # 设置 dtype
        dtype = {
            'wind_code': String(20),
            'key': String(80),
            'time': Date,
            'value': String(80),
            'param': String(80),

        }
        data_df_list, data_count, tot_data_count, code_count = [], 0, 0, len(code_date_range_dic)
        try:
            for num, (wind_code, (date_from, date_to)) in enumerate(code_date_range_dic.items(), start=1):
                logger.debug('%d/%d) %s [%s - %s]', num, code_count, wind_code, date_from, date_to)
                data_df = invoker.wsd(
                    wind_code,
                    col_name,
                    date_from,
                    date_to,
                    param
                )
                if data_df is not None or data_df.shape[0] > 0:
                    # 对我们的表格进行规范整理,整理我们的列名，索引更改
                    data_df['key'] = col_name
                    data_df['param'] = param
                    data_df['wind_code'] = wind_code
                    data_df.rename(columns={col_name.upper(): 'value'}, inplace=True)
                    data_df.index.rename('time', inplace=True)
                    data_df.reset_index(inplace=True)
                    data_count += data_df.shape[0]
                    data_df_list.append(data_df)

                # 大于阀值有开始插入
                if data_count >= 10000:
                    tot_data_df = pd.concat(data_df_list)
                    tot_data_df.to_sql(table_name, engine_md, if_exists='append', index=False, dtype=dtype)
                    tot_data_count += data_count
                    data_df_list, data_count = [], 0

                # 仅调试使用
                if DEBUG and len(data_df_list) > 1:
                    break

                all_finished = True
        finally:
            if data_count > 0:
                tot_data_df = pd.concat(data_df_list)
                tot_data_df.to_sql(table_name, engine_md, if_exists='append', index=False, dtype=dtype)
                tot_data_count += data_count

            if not has_table and engine_md.has_table(table_name):
                create_pk_str = """ALTER TABLE {table_name}
                    CHANGE COLUMN `wind_code` `wind_code` VARCHAR(20) NOT NULL ,
                    CHANGE COLUMN `time` `time` DATE NOT NULL ,
                    CHANGE COLUMN `key` `key` VARCHAR(80) NOT NULL ,
                    CHANGE COLUMN `param` `param` VARCHAR(80) NOT NULL ,
                    ADD PRIMARY KEY (`wind_code`, `time`, `key`, `param`)""".format(table_name=table_name)
                with with_db_session(engine_md) as session:
                    session.execute(create_pk_str)

            logging.info("更新 %s 完成 新增数据 %d 条", table_name, tot_data_count)

        return all_finished


if __name__ == "__main__":
    import_wind_stock_info(refresh=False)
    logging.basicConfig(level=logging.DEBUG, format=' %(asctime)s: %(levelname)s [%(name)s: %(funcName)s] %(message)s')
    DEBUG = True
    # 更新每日股票数据
    wind_code_set = None
    import_stock_daily()
    # import_stock_daily_wch()
    # add_new_col_data('ebitdaps', '',wind_code_set=wind_code_set)