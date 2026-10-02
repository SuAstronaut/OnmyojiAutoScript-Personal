# 新增发布碎片功能
增加DailyAltAcc 子功能
## 流程
 1. 检查DailyAltAcc目录下是否存在sr_cnt.json文件，如果存在则跳过2,3
 2. 从logs/sr_count.json读取name和count字段,使用name作为key,count作为value，创建list
 3. 遍历list,用count除去99(整数运算)，例如1000除99为10，100除99为1,98除99为0 然后按照count从大到小排序，如果count相同后来的排前面，count为零的name不插入list，将遍历结果保存在list中,将list保存在DailyAltAcc目录下sr_cnt.json文件中
 4. 读取sr_cnt.json文件内容,按照list顺序使用name进行模版匹配直到list遍历完结束匹配,匹配成功先点击资源然后进行碎片发布流程(碎片发布流程预留接口不用实现)并将匹配到的name的count减1,如果count为零则从list中删除该name,并将list重新排好序，写入到sr_cnt.json文件中。匹配失败直接结束该流程。